"""Health flow task definitions."""

from .broadcast import (
    _broadcast_logic,
    _can_use_broadcast_reliability_columns,
    _is_missing_broadcast_reliability_column,
    _set_broadcast_reliability_columns_available,
)
from .common import *


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
