"""Health flow task definitions."""

from .broadcast import (
    _broadcast_logic,
    _can_use_broadcast_reliability_columns,
    _is_missing_broadcast_reliability_column,
    _set_broadcast_reliability_columns_available,
)
from .common import *

# --- Health alerting ---------------------------------------------------------
# Each probe writes only Redis state; alerting happens once, after every probe
# has been evaluated. States are keyed per signal so a flapping signal cannot
# suppress a different signal's transition.
HEALTH_ALERT_STATE_PREFIX = "health:alert:state:"
HEALTH_ALERT_SIGNALS = ("bot_listener_lock", "heartbeat", "api_detailed")
HEALTH_ALERT_STATE_TTL_SECONDS = 30 * 24 * 3600
HEALTH_HEARTBEAT_KEY = "system:heartbeat:last_seen"
HEALTH_HEARTBEAT_STALE_SECONDS = 35 * 60
HEALTH_ALERT_API_TIMEOUT_SECONDS = 10.0
_BOT_POLL_LOCK_MATCH = "bot_listener:poll_lock:*"


def _health_api_base_url() -> str:
    """Resolve the internal API base from existing env conventions.

    ``API_BASE_URL`` is already used by ``scripts/ops/ops_report.py``; workers
    inside Compose can override it with the service DNS name. The fallback is
    the API service's in-network address (``api:8001``) rather than a public
    host, so no external endpoint is baked in.
    """
    from urllib.parse import urlsplit

    for env_name in ("API_BASE_URL", "API_INTERNAL_URL"):
        value = os.getenv(env_name)
        if value:
            return value.rstrip("/")

    webhook_url = os.getenv("HONEYPOT_WEBHOOK_URL")
    if webhook_url:
        parts = urlsplit(webhook_url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"

    return f"http://api:{os.getenv('API_PORT', '8001')}"


async def _probe_bot_listener_lock() -> dict[str, Any]:
    """Check that at least one bot poller holds a fresh poll lock.

    ``_renew_poll_lock`` refreshes the TTL every ``LOCK_TTL_SECONDS // 3``
    seconds, so a live poller always has a positive TTL bounded by the lock
    TTL. ``-1`` (no expiry) and ``-2`` (missing) are both stale.
    """
    from app.core.constants import LOCK_TTL_SECONDS

    keys = list(redis_client.scan_iter(match=_BOT_POLL_LOCK_MATCH, count=100))
    if not keys:
        return {"healthy": False, "detail": "no active poll lock"}

    fresh_ttls: list[int] = []
    stale_count = 0
    for key in keys:
        ttl = redis_client.ttl(key)
        if isinstance(ttl, int) and 0 < ttl <= LOCK_TTL_SECONDS:
            fresh_ttls.append(ttl)
        else:
            stale_count += 1

    if not fresh_ttls:
        return {
            "healthy": False,
            "detail": f"{len(keys)} poll lock(s) present but none fresh (stale={stale_count})",
        }
    return {
        "healthy": True,
        "detail": f"{len(fresh_ttls)} fresh poll lock(s), min_ttl={min(fresh_ttls)}s",
    }


async def _probe_heartbeat() -> dict[str, Any]:
    """Check ``system:heartbeat:last_seen`` freshness."""
    raw = redis_client.get(HEALTH_HEARTBEAT_KEY)
    if raw is None:
        return {"healthy": False, "detail": "heartbeat key missing"}
    try:
        last_seen = float(raw)
    except (TypeError, ValueError):
        return {"healthy": False, "detail": "heartbeat value not numeric"}

    age_seconds = max(0, int(time.time() - last_seen))
    healthy = age_seconds <= HEALTH_HEARTBEAT_STALE_SECONDS
    return {
        "healthy": healthy,
        "detail": (
            f"heartbeat age {age_seconds}s "
            f"(stale after {HEALTH_HEARTBEAT_STALE_SECONDS}s)"
        ),
    }


async def _probe_api_detailed() -> dict[str, Any]:
    """Call the local API's protected ``/health/detailed`` endpoint."""
    url = f"{_health_api_base_url()}/health/detailed"
    headers = {"X-Monitor-Key": settings.MONITOR_API_KEY}
    async with httpx.AsyncClient(timeout=HEALTH_ALERT_API_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=headers)

    if response.status_code != 200:
        return {
            "healthy": False,
            "detail": f"HTTP {response.status_code}",
        }
    try:
        payload = response.json()
    except Exception:
        return {"healthy": False, "detail": "invalid JSON from /health/detailed"}

    status = payload.get("status") if isinstance(payload, dict) else None
    return {
        "healthy": status == "healthy",
        "detail": f"status={status!r}",
    }


async def _run_health_probe(name: str, probe) -> dict[str, Any]:
    """Run one probe defensively — a single failure never aborts the others."""
    try:
        result = await probe()
    except Exception as exc:
        logger.warning(f"[HealthAlert] probe {name} failed: {type(exc).__name__}: {exc}")
        return {"healthy": False, "detail": f"probe error: {type(exc).__name__}"}
    if not isinstance(result, dict):
        return {"healthy": False, "detail": "probe returned invalid result"}
    result.setdefault("healthy", False)
    return result


def _read_health_alert_state(signal: str) -> str | None:
    try:
        return redis_client.get(f"{HEALTH_ALERT_STATE_PREFIX}{signal}")
    except Exception as exc:
        logger.warning(f"[HealthAlert] state read failed for {signal}: {exc}")
        return None


def _write_health_alert_state(signal: str, state: str) -> None:
    try:
        redis_client.setex(
            f"{HEALTH_ALERT_STATE_PREFIX}{signal}",
            HEALTH_ALERT_STATE_TTL_SECONDS,
            state,
        )
    except Exception as exc:
        logger.warning(f"[HealthAlert] state write failed for {signal}: {exc}")


async def _health_alert_logic() -> dict[str, Any]:
    probes = {
        "bot_listener_lock": _probe_bot_listener_lock,
        "heartbeat": _probe_heartbeat,
        "api_detailed": _probe_api_detailed,
    }
    names = list(HEALTH_ALERT_SIGNALS)
    results = await asyncio.gather(
        *(_run_health_probe(name, probes[name]) for name in names)
    )
    checks = dict(zip(names, results, strict=True))

    transitions: list[tuple[str, str, str]] = []
    baseline_states: list[tuple[str, str]] = []
    for name, result in checks.items():
        current = "healthy" if result.get("healthy") else "unhealthy"
        previous = _read_health_alert_state(name)
        if previous is None and current == "healthy":
            baseline_states.append((name, current))
            continue
        if previous != current:
            transitions.append((name, current, str(result.get("detail", ""))))

    if transitions:
        lines = ["Health state change:"]
        for name, current, detail in transitions:
            if current == "healthy":
                lines.append(f"✅ {name}: recovered to healthy")
            else:
                lines.append(f"❌ {name}: unhealthy — {detail}")
        try:
            await get_broadcaster().send_log("\n".join(lines))
        except Exception as exc:
            logger.warning(f"[HealthAlert] failed to post alert: {exc}")

        # Persist after the alert attempt (successful or not) so a failing
        # broadcaster cannot turn one transition into an alert storm.
        for name, current, _detail in transitions:
            _write_health_alert_state(name, current)

    for name, current in baseline_states:
        _write_health_alert_state(name, current)

    return {
        "checks": checks,
        "transitions": [
            {"signal": name, "state": current} for name, current, _detail in transitions
        ],
    }


@app.task(name="flow.health_alert")
def health_alert():
    """Alert on bot listener / heartbeat / API health state transitions."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_health_alert_logic())


@app.task(name="flow.system_heartbeat")
def system_heartbeat():
    """Periodic ping to confirm system uptime. Also flushes in-memory metrics to Redis."""
    msg = "💓 **System Heartbeat**: Worker is active and scanning."

    try:
        redis_client.set("system:heartbeat:last_seen", int(time.time()))
    except Exception as e:
        logger.warning(f"Failed to update heartbeat in Redis: {e}")

    # Flush in-memory metric counters to Redis so they survive restarts
    try:
        from app.core.metrics import metrics
        flushed = metrics.flush_to_redis()
        if not flushed:
            logger.warning("[Heartbeat] metrics.flush_to_redis() returned False — counters NOT persisted (Redis down?)")
    except Exception as e:
        logger.warning(f"Metrics flush failed (non-fatal): {e}")

    from app.workers.celery_app import get_worker_loop
    get_worker_loop().run_until_complete(get_broadcaster().send_log(msg))
    return "Heartbeat sent."

@app.task(name="flow.queue_monitor")
def queue_monitor():
    """Return queue depths and oldest tracked job age for operational monitoring."""
    try:
        from app.core.queue_monitor import (
            get_delivery_latency_summary,
            get_queue_snapshot,
            summarize_queue_health,
        )

        snapshot = get_queue_snapshot(redis_client)
        summary = summarize_queue_health(
            snapshot,
            length_threshold=settings.QUEUE_ALERT_LENGTH_THRESHOLD,
            oldest_age_threshold_seconds=settings.QUEUE_ALERT_OLDEST_AGE_SECONDS,
            delivery_latency=get_delivery_latency_summary(redis_client),
        )
        logger.info(f"[QueueMonitor] {summary}")
        if summary["alerts"]:
            lines = ["Queue monitor alert:"]
            for alert in summary["alerts"][:8]:
                lines.append(
                    f"- {alert['queue']} {alert['type']}: "
                    f"{alert['value']} >= {alert['threshold']}"
                )
            from app.workers.celery_app import get_worker_loop

            get_worker_loop().run_until_complete(get_broadcaster().send_log("\n".join(lines)))
        return summary
    except Exception as e:
        logger.warning(f"[QueueMonitor] failed: {e}")
        return {"error": str(e)}

@app.task(name="flow.canary_flow_check")
def canary_flow_check():
    """Run a synthetic DB -> broadcast -> visibility canary when configured."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_canary_flow_check_logic())

@app.task(name="flow.canary_findings_check")
def canary_findings_check():
    """LOGIC-001: end-to-end canary that DOES NOT require the raw broadcast
    pipeline. Inserts a synthetic finding, verifies it lands, then cleans up.

    Runs even when ENABLE_RAW_MESSAGE_BROADCAST is False (findings-first
    workflow default post-2026-09-06). Returns {status:'disabled', reason:
    'findings_table_missing'} gracefully if the DATA-001 migration hasn't
    been applied — so it never fails loudly on a fresh Supabase.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_canary_findings_check_logic())

async def _canary_findings_check_logic() -> dict:
    from datetime import datetime
    from uuid import uuid4

    canary_key = f"canary-findings-{uuid4()}"
    now = datetime.now(UTC)
    row = {
        "type": "canary",
        "title": "Synthetic canary — safe to ignore",
        "summary": "flow.canary_findings_check verification row",
        "priority": 1,
        "status": "triaged",
        "signature": canary_key,
        "created_at": now.isoformat(),
    }

    result: dict[str, Any] = {"status": "started", "signature": canary_key}
    try:
        try:
            insert_res = await async_execute(db.table("findings").insert(row))
        except Exception as insert_exc:
            exc_str = str(insert_exc)
            if "findings" in exc_str and ("does not exist" in exc_str or "PGRST205" in exc_str):
                return {
                    "status": "disabled",
                    "reason": "findings_table_missing",
                    "hint": (
                        "Apply supabase/migrations/20260904000002_insight_queue.sql "
                        "then re-enable this canary."
                    ),
                }
            raise

        inserted_rows = insert_res.data or []
        row_id = inserted_rows[0].get("id") if inserted_rows else None
        result["inserted"] = bool(row_id)

        # Read-back to prove the row landed.
        try:
            check = await async_execute(
                db.table("findings")
                .select("id, signature")
                .eq("signature", canary_key)
                .limit(1)
            )
            result["visible"] = bool(check.data)
        except Exception as check_exc:
            result["visible"] = False
            result["visible_error"] = str(check_exc)[:200]

        # Best-effort cleanup so the synthetic row doesn't pollute the queue.
        try:
            if row_id:
                await async_execute(
                    db.table("findings").delete().eq("id", row_id)
                )
            else:
                await async_execute(
                    db.table("findings").delete().eq("signature", canary_key)
                )
            result["cleaned_up"] = True
        except Exception as cleanup_exc:
            result["cleaned_up"] = False
            result["cleanup_error"] = str(cleanup_exc)[:200]

        result["status"] = "ok" if result.get("inserted") and result.get("visible") else "failed"
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)[:300]

    try:
        AuditLogger.log(
            AuditEvent.CANARY_FLOW_CHECK,
            details=result,
            success=(result["status"] == "ok"),
        )
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")

    return result

async def _canary_flow_check_logic():
    from datetime import datetime

    cred_id = settings.CANARY_CREDENTIAL_ID
    if not cred_id:
        return {"status": "disabled", "reason": "CANARY_CREDENTIAL_ID not configured"}
    if not settings.ENABLE_RAW_MESSAGE_BROADCAST:
        return {
            "status": "disabled",
            "reason": "raw message broadcast is intentionally disabled by policy",
        }

    now = datetime.now(UTC)
    CANARY_MSG_ID_BASE = 9_000_000_000_000_000
    telegram_msg_id = -(CANARY_MSG_ID_BASE + int(time.time()))
    content = f"{settings.CANARY_EXPECTED_TEXT} {now.isoformat()}"
    row = {
        "credential_id": cred_id,
        "telegram_msg_id": telegram_msg_id,
        "sender_name": "telegramhunter-canary",
        "content": content,
        "media_type": "text",
        "file_meta": {"source": "canary", "created_at": now.isoformat()},
        "is_broadcasted": False,
        "broadcast_error": None,
        "broadcast_attempts": 0,
        "next_retry_at": None,
    }
    legacy_row = {
        key: value
        for key, value in row.items()
        if key not in {"broadcast_error", "broadcast_attempts", "next_retry_at"}
    }
    result: dict[str, Any] = {
        "status": "started",
        "credential_id": cred_id,
        "telegram_msg_id": telegram_msg_id,
        "inserted": False,
        "broadcasted": False,
        "frontend_visible": None,
    }
    try:
        try:
            await async_execute(
                db.table("exfiltrated_messages").upsert(
                    row,
                    on_conflict="credential_id,telegram_msg_id",
                    ignore_duplicates=False,
                )
            )
            _set_broadcast_reliability_columns_available(True)
        except Exception as insert_exc:
            if not _is_missing_broadcast_reliability_column(insert_exc):
                raise
            _set_broadcast_reliability_columns_available(False)
            await async_execute(
                db.table("exfiltrated_messages").upsert(
                    legacy_row,
                    on_conflict="credential_id,telegram_msg_id",
                    ignore_duplicates=False,
                )
            )
        result["inserted"] = True

        result["broadcast_result"] = await _broadcast_logic()

        # Poll for is_broadcasted with a short budget so distributed-claim
        # contention doesn't false-fail. If another worker won the claim lock,
        # the row will flip to is_broadcasted=True moments later once that
        # worker's send completes. Stop early on success or persisted error.
        CANARY_BROADCAST_POLL_SECONDS = 15.0  # noqa: N806 — function-scoped constant
        CANARY_BROADCAST_POLL_INTERVAL = 3.0  # noqa: N806 — function-scoped constant
        elapsed = 0.0
        polls = 0
        rows: list[dict[str, Any]] = []
        while True:
            polls += 1
            select_columns = (
                "id,is_broadcasted,broadcast_error"
                if _can_use_broadcast_reliability_columns()
                else "id,is_broadcasted"
            )
            try:
                check = await async_execute(
                    db.table("exfiltrated_messages")
                    .select(select_columns)
                    .eq("credential_id", cred_id)
                    .eq("telegram_msg_id", telegram_msg_id)
                    .limit(1)
                )
                if "broadcast_error" in select_columns:
                    _set_broadcast_reliability_columns_available(True)
            except Exception as select_exc:
                if not _is_missing_broadcast_reliability_column(select_exc):
                    raise
                _set_broadcast_reliability_columns_available(False)
                check = await async_execute(
                    db.table("exfiltrated_messages")
                    .select("id,is_broadcasted")
                    .eq("credential_id", cred_id)
                    .eq("telegram_msg_id", telegram_msg_id)
                    .limit(1)
                )
            rows = check.data or []
            done = (
                not rows
                or bool(rows[0].get("is_broadcasted"))
                or rows[0].get("broadcast_error") is not None
                or elapsed >= CANARY_BROADCAST_POLL_SECONDS
            )
            if done:
                break
            await asyncio.sleep(CANARY_BROADCAST_POLL_INTERVAL)
            elapsed += CANARY_BROADCAST_POLL_INTERVAL

        result["broadcast_poll_seconds"] = elapsed
        result["broadcast_polls"] = polls
        if rows:
            result["broadcasted"] = bool(rows[0].get("is_broadcasted"))
            result["broadcast_error"] = rows[0].get("broadcast_error")

        if settings.PUBLIC_FRONTEND_URL:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(settings.PUBLIC_FRONTEND_URL)
                result["frontend_visible"] = response.status_code < 500
                result["frontend_status_code"] = response.status_code
            except Exception as frontend_exc:
                result["frontend_visible"] = False
                result["frontend_error"] = str(frontend_exc)[:300]

        passed = bool(result["inserted"] and result["broadcasted"])
        if result["frontend_visible"] is False:
            passed = False
        result["status"] = "ok" if passed else "failed"
        AuditLogger.log(
            AuditEvent.CANARY_FLOW_CHECK,
            credential_id=cred_id,
            details=result,
            success=passed,
        )
        return result
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)[:500]
        AuditLogger.log(
            AuditEvent.CANARY_FLOW_CHECK,
            credential_id=cred_id,
            details=result,
            success=False,
        )
        return result
