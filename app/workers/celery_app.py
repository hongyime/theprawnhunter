import asyncio
import logging
import os
import sys

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    before_task_publish,
    task_failure,
    task_prerun,
    worker_ready,
    worker_shutdown,
)

from app.core.config import settings

# ==============================================
# WORKER LOGGING CONFIGURATION
# ==============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = Celery("telegram_hunter", broker=settings.REDIS_URL, backend=settings.REDIS_URL)

# ==============================================
# PERSISTENT EVENT LOOP (BUG-008)
# One loop per worker process — avoids asyncio.run() creating a new loop per task,
# which broke asyncio.Lock objects and defeated Telethon connection pooling.
# ==============================================
_worker_loop: asyncio.AbstractEventLoop | None = None


def get_worker_loop() -> asyncio.AbstractEventLoop:
    """
    Returns the persistent event loop for this worker process.
    Creates one if it doesn't exist or was closed.
    All Celery tasks must use this loop via loop.run_until_complete()
    instead of asyncio.run().
    """
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
        logger.info(f"[Worker] Created persistent event loop (pid={os.getpid()})")
    return _worker_loop


def _run_sync(coro):
    """
    Run an async coroutine synchronously on the worker's persistent event loop.

    This is the canonical single definition — imported by scanner_tasks,
    audit_tasks, firehose_tasks, pivot_tasks, and validation_tasks.
    All local copies of this function in individual task modules should import
    from here instead of redefining it.
    """
    return get_worker_loop().run_until_complete(coro)


# ==============================================
# WORKER LIFECYCLE SIGNALS
# ==============================================

def _send_signal_log(msg: str):
    """Send a startup/shutdown notification to Telegram using the persistent loop."""
    loop = get_worker_loop()
    try:
        from app.services.broadcaster_srv import BroadcasterService
        broadcaster = BroadcasterService()
        loop.run_until_complete(asyncio.wait_for(broadcaster.send_log(msg), timeout=5.0))
    except TimeoutError:
        logger.warning(f"Signal notification timed out: {msg[:30]}...")
    except Exception as e:
        logger.warning(f"Signal notification failed: {e}")


@worker_ready.connect
def on_worker_ready(**kwargs):
    get_worker_loop()  # Ensure loop is initialized before any task runs

    # Wait for internet on startup (handles machine boot / container restart)
    from app.core.connectivity import wait_for_internet_sync
    if not wait_for_internet_sync(max_wait=300, check_interval=10):
        logger.warning("[Worker] Started without internet — tasks needing external APIs will wait.")

    # REL-006: log beat schedule digest so operators can verify the persisted
    # celerybeat-schedule file matches what the running code expects.
    try:
        import hashlib as _hashlib

        schedule = app.conf.get("beat_schedule") or {}
        task_names = sorted(schedule.keys())
        digest = _hashlib.sha256("|".join(task_names).encode()).hexdigest()[:12]
        logger.info(
            f"[Beat] schedule digest sha256={digest} tasks={len(task_names)}"
        )
    except Exception as _e:
        logger.debug(f"[Beat] schedule digest log failed: {_e}")

    _send_signal_log("🟢 **Worker Service** Started (Celery)")


@worker_shutdown.connect
def on_worker_shutdown(**kwargs):
    _send_signal_log("🔴 **Worker Service** Stopping...")
    global _worker_loop
    if _worker_loop and not _worker_loop.is_closed():
        _worker_loop.close()
        logger.info("[Worker] Persistent event loop closed.")


@task_failure.connect
def on_task_failure(task_id, exception, traceback, einfo, args, kwargs, **extra):
    """
    Fires when a task exhausts all retries and is permanently failed.
    Logs to audit_logs so failures are visible without a dead-letter queue.
    Routes through AuditLogger so the token-redaction pass runs on the
    exception string (URLs like /bot<TOKEN>/sendMessage → /bot<REDACTED>/).
    Non-blocking: uses a fire-and-forget thread so the Celery signal handler
    never blocks the worker event loop.
    """
    task_name = extra.get("sender", {})
    task_name = task_name.name if hasattr(task_name, "name") else str(task_name)

    exc_str = str(exception)[:500]  # cap to avoid huge audit rows
    logger.error(
        f"[DeadLetter] Task {task_name}[{task_id}] permanently failed: {exc_str}"
    )

    # Persist to audit_logs via AuditLogger (redaction pass applies)
    def _persist():
        try:
            from app.core.audit import AuditEvent, AuditLogger

            AuditLogger.log(
                AuditEvent.TASK_FAILURE if hasattr(AuditEvent, "TASK_FAILURE")
                else "task_permanent_failure",
                details={
                    "task_name": task_name,
                    "task_id": task_id,
                    "exception": exc_str,
                },
                user="celery_worker",
                success=False,
            )
        except Exception as e:
            logger.warning(f"[DeadLetter] Could not persist failure to audit_logs: {e}")

    import threading
    threading.Thread(target=_persist, daemon=True).start()


@before_task_publish.connect
def on_before_task_publish(sender=None, headers=None, body=None, routing_key=None, **kwargs):
    """Track queue age independently of broker internals."""
    try:
        task_id = (headers or {}).get("id")
        queue_name = routing_key or (headers or {}).get("queue") or "celery"
        import redis as _redis

        from app.core.queue_monitor import record_task_enqueued

        client = _redis.from_url(settings.REDIS_URL, decode_responses=True)
        record_task_enqueued(client, queue_name, task_id)
    except Exception as e:
        logger.debug(f"[QueueMonitor] enqueue tracking skipped: {e}")


@task_prerun.connect
def on_task_prerun(task_id, task, args, kwargs, **extra):
    """
    Gate task execution on internet connectivity.
    Waits up to 120s for connection before letting the task proceed.
    Tasks that only need Redis (heartbeat) are exempted.
    """
    try:
        import redis as _redis

        from app.core.queue_monitor import record_task_started

        client = _redis.from_url(settings.REDIS_URL, decode_responses=True)
        record_task_started(client, task_id)
    except Exception as e:
        logger.debug(f"[QueueMonitor] start tracking skipped: {e}")

    # Exempt local-only tasks that don't need external APIs
    local_only_tasks = {"flow.system_heartbeat"}
    if task.name in local_only_tasks:
        return

    from app.core.connectivity import check_internet, wait_for_internet_sync
    if not check_internet(timeout=3):
        logger.warning(f"[PreRun] No internet for task {task.name} — waiting...")
        wait_for_internet_sync(max_wait=120, check_interval=10)


# ==============================================
# CELERY CONFIGURATION
# ==============================================
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    # ============================================
    # Local Docker Deployment (Aggressive Mode)
    # ============================================
    result_expires=1800,
    task_ignore_result=True,
    # Recycle well below the container memory limit. The old 800 MB value was
    # above the 768 MB compose limit, so the OOM killer always won the race.
    worker_max_memory_per_child=int(os.getenv("WORKER_MAX_MEMORY_PER_CHILD_KB", "600000")),
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_soft_time_limit=1200,  # 20 minutes soft limit
    task_time_limit=1800,       # Hard limit — 10 min window after soft for graceful save
    broker_pool_limit=10,
    # Auto-discover tasks in these modules
    imports=[
        "app.workers.tasks.flows.reports",
        "app.workers.tasks.flows.diagnostics",
        "app.workers.tasks.flows.media",
        "app.workers.tasks.flows.findings",
        "app.workers.tasks.flows.broadcast",
        "app.workers.tasks.flows.health",
        "app.workers.tasks.flows.webhooks",
        "app.workers.tasks.flows.entities",
        "app.workers.tasks.flows.scrape",
        "app.workers.tasks.flows.honeypot",
        "app.workers.tasks.flows.help_text",
        "app.workers.tasks.flows.virustotal",
        "app.workers.tasks.flows.api_keys",
        "app.workers.tasks.flows.session_admins",
        "app.workers.tasks.flows.watchlist_backfill",
        "app.workers.tasks.flows.heavy",
        "app.workers.tasks.flow_tasks",  # stable import facade for existing callers
        "app.workers.tasks.scanners.providers.shodan",
        "app.workers.tasks.scanners.providers.urlscan",
        "app.workers.tasks.scanners.providers.github",
        "app.workers.tasks.scanners.providers.fofa",
        "app.workers.tasks.scanners.providers.gitlab",
        "app.workers.tasks.scanners.providers.bitbucket",
        "app.workers.tasks.scanners.providers.github_gist",
        "app.workers.tasks.scanners.providers.grepapp",
        "app.workers.tasks.scanners.providers.publicwww",
        "app.workers.tasks.scanners.providers.pastebin",
        "app.workers.tasks.scanners.providers.rentry",
        "app.workers.tasks.scanners.providers.hastebin",
        "app.workers.tasks.scanners.providers.exa",
        "app.workers.tasks.scanners.providers.cold",
        "app.workers.tasks.scanners.providers.google",
        "app.workers.tasks.scanners.providers.netlas",
        "app.workers.tasks.scanners.providers.wayback",
        "app.workers.tasks.scanners.providers.telegram_search",
        "app.workers.tasks.scanners.providers.commoncrawl",
        "app.workers.tasks.scanners.providers.sourcegraph",
        "app.workers.tasks.scanners.providers.replit",
        "app.workers.tasks.scanners.providers.postman",
        "app.workers.tasks.scanners.providers.dockerhub",
        "app.workers.tasks.scanners.providers.searchcode",
        "app.workers.tasks.scanners.providers.searxng",
        "app.workers.tasks.scanners.providers.npm",
        "app.workers.tasks.scanners.providers.pypi",
        "app.workers.tasks.scanners.providers.ci_logs",
        "app.workers.tasks.scanner_tasks",
        "app.workers.tasks.audit_tasks",
        "app.workers.tasks.import_tasks",   # CSV import pipeline
        "app.workers.tasks.validation_tasks",  # async token validation (off scanner critical path)
        "app.workers.tasks.pivot_tasks",       # Bundle 1: pivot fan-out from validator
        "app.workers.tasks.firehose_tasks",    # Bundle 2: GitHub Events real-time firehose
        "app.workers.tasks.honeypot_redirect_tasks",  # Multi-touch redirect reminder tasks
        "app.workers.tasks.retention_tasks",  # Automated storage retention cleanup
    ],
    # ============================================
    # QUEUE SEGREGATION
    # ============================================
    task_routes={
        "flow.exfiltrate_chat": {"queue": "scrape"},
        "flow.rescrape_active": {"queue": "scrape"},
        "scanner.*": {"queue": "scanners"},
        "validation.*": {"queue": "validation"},
        "pivot.*": {"queue": "validation"},
        "firehose.*": {"queue": "scanners"},
        "heavy.*": {"queue": "heavy"},
    },
    beat_schedule={
        # ============================================
        # BROADCAST & RESCRAPE
        # ============================================
        "broadcast-every-minute": {
            "task": "flow.broadcast_pending",
            # Default every 1 minute. If BROADCAST_INTERVAL_MINUTES=1 and batch=100 msgs × 2s sleep
            # the task can run up to ~200s. Lock TTL (set in broadcast_pending) must exceed that.
            "schedule": crontab(minute=f"*/{int(os.getenv('BROADCAST_INTERVAL_MINUTES', 1))}"),
        },
        # GitHub Events firehose — real-time leak detection (every 30s)
        # Public timeline polling, ETag-aware. ~5000 req/hr GitHub quota.
        # Catches leaks within ~30s of push, vs 6+ min for /search/code indexing.
        "firehose-github-events-30s": {
            "task": "firehose.poll_github_events",
            "schedule": 30.0,  # raw seconds — fires every 30s
        },
        "rescrape-active-hourly": {
            "task": "flow.rescrape_active",
            "schedule": crontab(minute=0, hour=f"*/{int(os.getenv('RESCRAPE_INTERVAL_HOURS', 1))}"),
        },
        # Heartbeat every 30 minutes
        "system-heartbeat-30min": {
            "task": "flow.system_heartbeat",
            "schedule": crontab(minute="*/30"),
        },
        "queue-monitor-5min": {
            "task": "flow.queue_monitor",
            "schedule": crontab(minute="*/5"),
        },
        "health-alert-5min": {
            "task": "flow.health_alert",
            "schedule": crontab(minute="*/5"),
        },
        "close-revoked-topics-5min": {
            "task": "flow.close_revoked_topics",
            "schedule": crontab(
                minute=f"*/{int(os.getenv('REVOKED_TOPIC_CLOSE_INTERVAL_MINUTES', 5))}"
            ),
            "kwargs": {
                "limit": int(os.getenv("REVOKED_TOPIC_CLOSE_BATCH_SIZE", 25)),
                "dry_run": False,
            },
        },
        "mute-monitor-topics-daily": {
            "task": "flow.mute_monitor_topics",
            "schedule": crontab(minute=10, hour=4),
        },
        "canary-flow-check-30min": {
            "task": "flow.canary_flow_check",
            "schedule": crontab(minute="*/30"),
        },
        # LOGIC-001: findings-pipeline canary. Independent of raw broadcast so
        # the ENABLE_RAW_MESSAGE_BROADCAST toggle doesn't blind operators to
        # findings-workflow health.
        "canary-findings-check-hourly": {
            "task": "flow.canary_findings_check",
            "schedule": crontab(minute=7, hour="*"),
        },
        # Passive fingerprint of captured third-party webhook URLs — every 6h,
        # 15 min after the hour to avoid colliding with other beats.
        "probe-webhooks-6hours": {
            "task": "flow.probe_webhooks",
            "schedule": crontab(minute=15, hour="*/6"),
        },
        # Force takeover pass — 30 min after each webhook probe, queues immediate
        # rescrape (and pin+delete) for every credential that still holds a
        # captured webhook URL. Every 6h.
        "force-webhook-takeover-6hours": {
            "task": "flow.force_webhook_takeover_pass",
            "schedule": crontab(minute=45, hour="*/6"),
            "kwargs": {"max_credentials": 200},
        },
        # Pin metrics report — broadcast takeover coverage + top C2 hosts. Every 12h.
        "report-pin-metrics-12hours": {
            "task": "flow.report_pin_metrics",
            "schedule": crontab(minute=5, hour="*/12"),
        },
        # Reclassify credentials that aren't 'active' or 'revoked' — daily @ 03:20 UTC
        "reclassify-dark-matter-daily": {
            "task": "flow.reclassify_dark_matter",
            "schedule": crontab(minute=20, hour=3),
            "kwargs": {"max_credentials": 500},
        },
        # Persistent Insight Queue producers — bounded and idempotent.
        "produce-findings-15min": {
            "task": "flow.produce_findings",
            "schedule": crontab(minute="*/15"),
            "kwargs": {"credential_limit": 2000, "message_limit": 50000},
        },
        "build-entity-graph-hourly": {
            "task": "flow.build_entity_graph",
            "schedule": crontab(minute=35, hour="*"),
            "kwargs": {"credential_limit": 2000, "evidence_limit": 50000},
        },
        "route-finding-deltas-5min": {
            "task": "flow.route_finding_deltas",
            "schedule": crontab(minute="*/5"),
        },
        "daily-top-findings-0805-utc": {
            "task": "flow.daily_findings_digest",
            "schedule": crontab(minute=5, hour=8),
        },
        "weekly-finding-alert-coverage": {
            "task": "flow.weekly_finding_alerts",
            "schedule": crontab(minute=20, hour=8, day_of_week=0),
        },
        # Source quality scorecard — weekly on Mondays @ 07:30 UTC
        "source-quality-weekly": {
            "task": "flow.source_quality_report",
            "schedule": crontab(minute=30, hour=7, day_of_week=1),
        },
        # Attribution graph — weekly on Tuesdays @ 08:00 UTC
        "attribution-graph-weekly": {
            "task": "flow.attribution_graph_report",
            "schedule": crontab(minute=0, hour=8, day_of_week=2),
        },
        # Honeypot redirect sweep — sends redirect messages to captured users.
        # Every 30 seconds for real-time response to honeypot captures.
        "honeypot-redirect-sweep-30s": {
            "task": "flow.honeypot_redirect_sweep",
            "schedule": 30.0,
        },
        "honeypot-evidence-batch-daily": {
            "task": "flow.honeypot_evidence_batch",
            "schedule": crontab(minute=40, hour=3),
            "kwargs": {"dry_run": False},
        },
        "membership-rotation-15min": {
            "task": "validation.membership_rotation",
            "schedule": crontab(minute="*/15"),
        },
        # Level 2: Multi-touch redirect reminder sweeps (every 24h).
        # Sends second and third reminder messages to users who didn't migrate.
        # Touch 2: users who received redirect_1 but not redirect_2 (24h+ ago)
        "honeypot-redirect-touch2-daily": {
            "task": "flow.honeypot_redirect_touch2",
            "schedule": crontab(minute=0, hour=9),  # Daily @ 09:00 UTC
        },
        # Touch 3: users who received redirect_2 but not redirect_3 (24h+ ago)
        "honeypot-redirect-touch3-daily": {
            "task": "flow.honeypot_redirect_touch3",
            "schedule": crontab(minute=0, hour=10),  # Daily @ 10:00 UTC
        },
        # Level 5: Proactive outreach via captured bot (every 6h).
        # Sends inline mode request to users who haven't been redirected.
        # Option A only (inline mode first approach).
        "honeypot-proactive-outreach-6h": {
            "task": "flow.honeypot_proactive_outreach",
            "schedule": crontab(minute=30, hour="*/6"),  # Every 6h @ :30
        },
        # C2 operator clusters — daily @ 08:00 UTC. Ranks hosted-service
        # tenants (railway/onrender/etc), Shodan orgs, and hostname operators.
        "cluster-c2-operators-daily": {
            "task": "flow.cluster_c2_operators",
            "schedule": crontab(minute=0, hour=8),
        },
        # Media forensics — hash new photos/documents every 30 min.
        # Enables cross-bot duplicate detection (same photo → common operator).
        "hash-exfil-media-30min": {
            "task": "flow.hash_exfil_media",
            "schedule": crontab(minute="*/30"),
            "kwargs": {"max_messages": 100},
        },
        # Reconcile topics from DB (source of truth) — hourly.
        # Ensures pending broadcasts get processed; does NOT reset already-
        # broadcasted messages (avoids duplicate flood).
        "reconcile-topics-hourly": {
            "task": "flow.reconcile_topics_from_db",
            "schedule": crontab(minute=25, hour="*"),
        },
        # User-agent group membership audit — verifies every active session's
        # account is still in the monitor group + has minimal admin perms.
        # Every 30 min.
        "audit-user-agent-membership-30min": {
            "task": "flow.audit_user_agent_group_membership",
            "schedule": crontab(minute="*/30"),
        },
        # Duplicate report — daily @ 08:15 UTC.
        "media-duplicate-report-daily": {
            "task": "flow.media_duplicate_report",
            "schedule": crontab(minute=15, hour=8),
        },
        # Observability — detect webhook takeover spikes indicating a mass
        # exposure event. Reads audit_logs event_type='webhook.takeover' count
        # over the last hour and alerts if > 20. Runs every 15 min.
        "takeover-spike-check-15min": {
            "task": "flow.takeover_spike_check",
            "schedule": crontab(minute="*/15"),
        },
        # Observability — exfiltration latency percentile report (P50/P95/P99)
        # over the last 1000 messages. Runs every 6h at minute :10.
        "exfil-latency-report-6hours": {
            "task": "flow.exfil_latency_report",
            "schedule": crontab(minute=10, hour="*/6"),
        },
        # Automated retention cleanup — daily @ 03:00 UTC to maintain database
        # size under Supabase free-tier limits without manual intervention.
        "watchlist-backfill-6hours": {
            "task": "flow.backfill_watchlists",
            "schedule": crontab(minute=25, hour="*/6"),
        },
        "ensure-session-admins-6hours": {
            "task": "flow.ensure_session_admins",
            "schedule": crontab(minute=40, hour="*/6"),
        },
        "api-key-health-daily": {
            "task": "flow.api_key_health",
            "schedule": crontab(minute=15, hour=7),
        },
        "virustotal-enrich-6hours": {
            "task": "flow.virustotal_enrich",
            "schedule": crontab(minute=50, hour="*/6"),
        },
        "retention-cleanup-daily": {
            "task": "flow.automated_retention_cleanup",
            "schedule": crontab(minute=0, hour=3),
        },
        # Periodic Help Guide (Every 6 hours)
        "system-help-6hours": {
            "task": "flow.system_help",
            "schedule": crontab(minute=30, hour="*/6"),
        },
        # ============================================
        # STAGGERED SCANS
        # ============================================
        "scan-github-4hours": {
            "task": "scanner.scan_github",
            "schedule": crontab(minute=0, hour=f"*/{int(os.getenv('SCAN_INTERVAL_HOURS', 4))}"),
        },
        "scan-shodan-4hours": {
            "task": "scanner.scan_shodan",
            "schedule": crontab(minute=20, hour=f"*/{int(os.getenv('SCAN_INTERVAL_HOURS', 4))}"),
        },
        "scan-urlscan-4hours": {
            "task": "scanner.scan_urlscan",
            "schedule": crontab(minute=40, hour=f"*/{int(os.getenv('SCAN_INTERVAL_HOURS', 4))}"),
        },
        # FOFA — key-gated: FofaService returns [] with no FOFA_EMAIL/FOFA_KEY,
        # so this entry is inert until a key is configured.
        "scan-fofa-4hours": {
            "task": "scanner.scan_fofa",
            "schedule": crontab(minute=15, hour="*/4"),
        },
        # scan-gitlab-6hours: DISABLED — gitlab.com free tier has global blob
        # search disabled (returns "403 Forbidden - Global Search is disabled
        # for this scope"). Re-enable only if upgrading to paid GitLab plan
        # OR refactoring scanner to project-scoped search.
        "scan-grepapp-6hours": {
            "task": "scanner.scan_grepapp",
            "schedule": crontab(minute=25, hour="*/6"),
        },
        "scan-gist-6hours": {
            "task": "scanner.scan_gist",
            "schedule": crontab(minute=45, hour="*/6"),
        },
        # scan-pastebin-12hours: DISABLED — Pastebin scraping API requires
        # paid IP whitelist ($30 + manual approval). Exa scanner already
        # covers pastebin.com via includeDomains with full content extraction.
        "scan-exa-12hours": {
            "task": "scanner.scan_exa",
            "schedule": crontab(minute=35, hour="*/12"),
        },
        "scan-rentry-12hours": {
            "task": "scanner.scan_rentry",
            "schedule": crontab(minute=40, hour="*/12"),
        },
        "scan-hastebin-12hours": {
            "task": "scanner.scan_hastebin",
            "schedule": crontab(minute=45, hour="*/12"),
        },
        # Wayback Machine — historical URL scanner (free, no key)
        # 04:00 UTC slot avoids overlap with regular scanners + quietest period
        # for archive.org's ~1 req/sec courtesy budget.
        "scan-wayback-daily": {
            "task": "scanner.scan_wayback",
            "schedule": crontab(minute=0, hour=4),
        },
        # Telegram MTProto self-search — uses UserAgent session to query
        # Telegram's own message index. 12h cadence respects per-account
        # FloodWait budget. Catches leaks discussed in public channels.
        "scan-telegram-search-12hours": {
            "task": "scanner.scan_telegram_search",
            "schedule": crontab(minute=20, hour="*/12"),
        },
        # Bundle 2.2: re-validate pending tokens daily — recovers chat_id
        # for bots that activated AFTER initial discovery (dormant→active).
        # 05:00 UTC = quiet period for Telegram getMe budget.
        "validation-refresh-pending-daily": {
            "task": "validation.refresh_pending_tokens",
            "schedule": crontab(minute=0, hour=5),
        },
        # Backfill scoring: runs every 10min, processes 50 rows/batch, self-terminates when done
        "validation-backfill-scoring": {
            "task": "validation.backfill_scoring",
            "schedule": crontab(minute="*/10"),
        },
        # Common Crawl — petabyte-scale historical web crawl, free index API.
        # Daily backfill from latest crawl. ~500 URLs/run, ~2 min runtime.
        "scan-commoncrawl-daily": {
            "task": "scanner.scan_commoncrawl",
            "schedule": crontab(minute=0, hour=2),  # 02:00 UTC
        },
        "scan-dockerhub-hourly": {
            "task": "scanner.scan_dockerhub",
            "schedule": crontab(minute=10),
        },
        # Sourcegraph — public code search over ~91k indexed repos with
        # api.telegram.org. Free, no auth. SSE stream search.
        "scan-sourcegraph-12hours": {
            "task": "scanner.scan_sourcegraph",
            "schedule": crontab(minute=10, hour="*/12"),
        },
        # scan-google-12hours: DISABLED — GCP project access issue, replaced by Exa.
        # Re-enable by uncommenting once Custom Search API is properly bound to billing.
        "scan-searchcode-every-8-hours": {
            "task": "scanner.scan_searchcode",
            "schedule": crontab(minute=5, hour="*/8"),
        },
        # PublicWWW — HTML source code search (free tier 200 req/day)
        "scan-publicwww-12hours": {
            "task": "scanner.scan_publicwww",
            "schedule": crontab(minute=15, hour="*/12"),
        },
        "scan-shodan-c2-6hours": {
            "task": "scanner.scan_shodan_c2",
            "schedule": crontab(minute=10, hour="*/6"),
        },
        # scan-replit-12hours: DISABLED — Replit now requires Apollo persisted
        # query hashes + session cookie. Public GraphQL search no longer works.
        # Postman public workspaces — free, no key. 12h cadence.
        "scan-postman-12hours": {
            "task": "scanner.scan_postman",
            "schedule": crontab(minute=50, hour="*/12"),
        },
        # Netlas — once daily (budget: 45+90=135 req/day across 2 accounts)
        "scan-netlas-daily": {
            "task": "scanner.scan_netlas",
            "schedule": crontab(minute=0, hour=3),
        },
        # npm registry — key-free tarball sweep. Every 6h at :10.
        "scan-npm-6hours": {
            "task": "scanner.scan_npm",
            "schedule": crontab(minute=10, hour="*/6"),
        },
        # PyPI package index — key-free sdist sweep. Every 6h at :35.
        "scan-pypi-6hours": {
            "task": "scanner.scan_pypi",
            "schedule": crontab(minute=35, hour="*/6"),
        },
        # GitHub Actions CI logs — bounded run-log sweep, daily @ 06:20 UTC.
        "scan-ci-logs-daily": {
            "task": "scanner.scan_ci_logs",
            "schedule": crontab(minute=20, hour=6),
        },
        # Validation dispatch — drains the priority staging queue every 15s.
        "validation-dispatch-15s": {
            "task": "validation.dispatch_pending",
            "schedule": 15.0,  # raw seconds — fires every 15s
        },
        # ============================================
        # RETRY COLD TOKENS
        # ============================================
        "scan-searxng-6hours": {
            "task": "scanner.scan_searxng",
            "schedule": crontab(minute=55, hour="*/6"),
        },
        "retry-cold-12hours": {
            "task": "scanner.retry_cold",
            "schedule": crontab(minute=50, hour="*/12"),
        },
        # ============================================
        # SYSTEM AUDIT, SELF-HEAL & FAILSAFES
        # ============================================
        "audit-active-topics-hourly": {
            "task": "audit.audit_active_topics",
            "schedule": crontab(minute=15, hour=f"*/{int(os.getenv('AUDIT_INTERVAL_HOURS', 1))}"),
        },
        "system-self-heal-90min": {
            "task": "system.self_heal",
            "schedule": 5400.0,  # 90 minutes in seconds
        },
        "system-enforce-whitelist-6hours": {
            "task": "system.enforce_whitelist",
            "schedule": crontab(minute=0, hour="1-23/6"),
        },
        "cleanup-general-topic-hourly": {
            "task": "system.cleanup_general_topic",
            "schedule": crontab(minute=30),
        },
        # Prune audit_logs entries older than 90 days — weekly, Sunday 03:30 UTC.
        # TOKEN_DECRYPTED fires on every broadcast run so table grows fast without this.
        "prune-audit-logs-weekly": {
            "task": "audit.prune_audit_logs",
            "schedule": crontab(minute=30, hour=3, day_of_week=0),
        },
        # Evict any Matkap victim bots left in monitor group after worker crash.
        # Runs hourly; silent if no pending sentinels exist.
        "cleanup-matkap-bots-hourly": {
            "task": "audit.cleanup_matkap_bots",
            "schedule": crontab(minute=45),
        },

        # CSV IMPORT PIPELINE (MISSING-001)
        # ============================================
        "import-csv-5min": {
            "task": "system.import_csv",
            "schedule": crontab(minute="*/5"),
        },
    },
)

# Stale-beat guard: a periodic task that waited in the broker past its useful
# window must be discarded, not executed late. Without this, one delayed beat
# can amplify a backlog (e.g. a stale force-webhook takeover enqueuing 200
# exfiltrations long after its scheduled run).
_BEAT_EXPIRY_SECONDS: dict[str, int] = {
    "broadcast-every-minute": 180,
    "firehose-github-events-30s": 25,
    "rescrape-active-hourly": 600,
    "system-heartbeat-30min": 300,
    "queue-monitor-5min": 240,
    "health-alert-5min": 240,
    "close-revoked-topics-5min": 240,
    "mute-monitor-topics-daily": 1800,
    "canary-flow-check-30min": 300,
    "canary-findings-check-hourly": 600,
    "probe-webhooks-6hours": 900,
    "force-webhook-takeover-6hours": 600,
    "report-pin-metrics-12hours": 1800,
    "reclassify-dark-matter-daily": 1800,
    "produce-findings-15min": 600,
    "build-entity-graph-hourly": 600,
    "route-finding-deltas-5min": 240,
    "daily-top-findings-0805-utc": 1800,
    "weekly-finding-alert-coverage": 3600,
    "source-quality-weekly": 3600,
    "attribution-graph-weekly": 3600,
    "honeypot-redirect-sweep-30s": 25,
    "honeypot-redirect-touch2-daily": 1800,
    "honeypot-redirect-touch3-daily": 1800,
    "honeypot-proactive-outreach-6h": 900,
    "cluster-c2-operators-daily": 1800,
    "hash-exfil-media-30min": 300,
    "reconcile-topics-hourly": 600,
    "audit-user-agent-membership-30min": 300,
    "media-duplicate-report-daily": 1800,
    "takeover-spike-check-15min": 300,
    "exfil-latency-report-6hours": 900,
    "retention-cleanup-daily": 1800,
    "virustotal-enrich-6hours": 1800,
    "api-key-health-daily": 3600,
    "ensure-session-admins-6hours": 600,
    "watchlist-backfill-6hours": 3600,
    "system-help-6hours": 900,
    "validation-refresh-pending-daily": 1800,
    "validation-backfill-scoring": 300,
    "audit-active-topics-hourly": 600,
    "system-self-heal-90min": 1800,
    "system-enforce-whitelist-6hours": 900,
    "cleanup-general-topic-hourly": 600,
    "prune-audit-logs-weekly": 3600,
    "cleanup-matkap-bots-hourly": 600,
    "import-csv-5min": 240,
    "retry-cold-12hours": 1800,
    "scan-searxng-6hours": 900,
    "honeypot-evidence-batch-daily": 7200,
    "membership-rotation-15min": 1800,
    "scan-github-4hours": 900,
    "scan-shodan-4hours": 900,
    "scan-urlscan-4hours": 900,
    "scan-grepapp-6hours": 900,
    "scan-gist-6hours": 900,
    "scan-exa-12hours": 1800,
    "scan-rentry-12hours": 1800,
    "scan-hastebin-12hours": 1800,
    "scan-wayback-daily": 7200,
    "scan-telegram-search-12hours": 1800,
    "scan-commoncrawl-daily": 7200,
    "scan-dockerhub-hourly": 600,
    "scan-sourcegraph-12hours": 1800,
    "scan-searchcode-every-8-hours": 900,
    "scan-fofa-4hours": 900,
    "scan-publicwww-12hours": 1800,
    "scan-shodan-c2-6hours": 900,
    "scan-postman-12hours": 1800,
    "scan-netlas-daily": 7200,
    "scan-npm-6hours": 900,
    "scan-pypi-6hours": 900,
    "scan-ci-logs-daily": 7200,
    "validation-dispatch-15s": 12,
}
for _beat_name, _expires in _BEAT_EXPIRY_SECONDS.items():
    app.conf.beat_schedule[_beat_name].setdefault("options", {})["expires"] = _expires
