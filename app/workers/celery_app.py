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
    worker_max_memory_per_child=800000,  # 800MB per worker
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_soft_time_limit=1200,  # 20 minutes soft limit
    task_time_limit=1800,       # Hard limit — 10 min window after soft for graceful save
    broker_pool_limit=10,
    # Auto-discover tasks in these modules
    imports=[
        "app.workers.tasks.flow_tasks",
        "app.workers.tasks.scanner_tasks",
        "app.workers.tasks.audit_tasks",
        "app.workers.tasks.import_tasks",   # CSV import pipeline
        "app.workers.tasks.validation_tasks",  # async token validation (off scanner critical path)
        "app.workers.tasks.pivot_tasks",       # Bundle 1: pivot fan-out from validator
        "app.workers.tasks.firehose_tasks",    # Bundle 2: GitHub Events real-time firehose
        "app.workers.tasks.honeypot_redirect_tasks",  # Multi-touch redirect reminder tasks
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
        "canary-flow-check-30min": {
            "task": "flow.canary_flow_check",
            "schedule": crontab(minute="*/30"),
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
        # scan-fofa-4hours: DISABLED — F-coins balance exhausted (F点余额不足).
        # Re-enable after topping up FOFA credits at fofa.info.
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
        "scan-bitbucket-8hours": {
            "task": "scanner.scan_bitbucket",
            "schedule": crontab(minute=30, hour="*/8"),
        },
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
        # ============================================
        # RETRY COLD TOKENS
        # ============================================
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
