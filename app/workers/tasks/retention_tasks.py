"""Automated database retention and storage cleanup.

Prunes aged rows from high-growth tables directly to maintain database size
under Supabase free-tier storage limits (500 MB).
"""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from app.core.config import settings
from app.core.database import db
from app.workers.celery_app import _run_sync, app
from app.workers.tasks.flow_tasks import async_execute, get_broadcaster

logger = logging.getLogger("retention.tasks")
logger.setLevel(logging.INFO)


@app.task(name="flow.automated_retention_cleanup")
def automated_retention_cleanup():
    """Run scheduled retention cleanup across operational database tables."""
    return _run_sync(_automated_retention_cleanup_async())


async def _automated_retention_cleanup_async() -> dict[str, Any]:
    if not getattr(settings, "RETENTION_CLEANUP_ENABLED", True):
        logger.info("[Retention] Cleanup disabled by configuration.")
        return {"status": "disabled"}

    now = datetime.now(UTC)
    results: dict[str, Any] = {}

    audit_days = max(1, int(getattr(settings, "RETENTION_AUDIT_LOG_DAYS", 14)))
    audit_cutoff = (now - timedelta(days=audit_days)).isoformat()

    keepalive_days = max(1, int(getattr(settings, "RETENTION_KEEPALIVE_DAYS", 7)))
    keepalive_cutoff = (now - timedelta(days=keepalive_days)).isoformat()

    hp_days = max(1, int(getattr(settings, "RETENTION_HONEYPOT_DAYS", 30)))
    hp_cutoff = (now - timedelta(days=hp_days)).isoformat()

    msg_days = max(1, int(getattr(settings, "RETENTION_EXFIL_MESSAGE_DAYS", 30)))
    msg_cutoff = (now - timedelta(days=msg_days)).isoformat()

    MAX_RETENTION_FETCH_LIMIT = 5000
    MIN_RETENTION_FETCH_LIMIT = 100
    configured_batch = int(getattr(settings, "RETENTION_BATCH_SIZE", 1000))
    batch_size = max(MIN_RETENTION_FETCH_LIMIT, min(MAX_RETENTION_FETCH_LIMIT, configured_batch))

    async def _delete_in_batches(table_name: str, build_query_fn) -> int:
        deleted_total = 0
        CHUNK_SIZE = 100  # Avoid HTTP 414 Request-URI Too Large (100 UUIDs ~ 3.7 KB)
        for _ in range(10):  # Bounded to max 10 chunks per invocation to prevent long-running worker stalls
            q = build_query_fn(db.table(table_name).select("id")).limit(batch_size)
            res = await async_execute(q)
            rows = res.data or []
            if not rows:
                break
            ids = [r["id"] for r in rows if "id" in r]
            if not ids:
                break
            for i in range(0, len(ids), CHUNK_SIZE):
                chunk = ids[i : i + CHUNK_SIZE]
                await async_execute(db.table(table_name).delete().in_("id", chunk))
            deleted_total += len(ids)
            if len(ids) < batch_size:
                break
        return deleted_total

    # 1. Audit logs (> 14 days)
    try:
        results["audit_logs"] = await _delete_in_batches(
            "audit_logs", lambda q: q.lt("timestamp", audit_cutoff)
        )
    except Exception as e:
        logger.warning(f"[Retention] audit_logs prune (timestamp) error: {e}")
        try:
            results["audit_logs"] = await _delete_in_batches(
                "audit_logs", lambda q: q.lt("created_at", audit_cutoff)
            )
        except Exception as e2:
            results["audit_logs_error"] = str(e2)

    # 2. Keepalive log (> 7 days)
    try:
        results["keepalive_log"] = await _delete_in_batches(
            "keepalive_log", lambda q: q.lt("created_at", keepalive_cutoff)
        )
    except Exception as e:
        logger.debug(f"[Retention] keepalive_log prune error: {e}")
        results["keepalive_log_error"] = str(e)

    # 3. Honeypot updates:
    #    a) Redirected updates (> 30 days)
    #    b) Unredirected/stale updates (> 60 days) to prevent infinite accumulation
    hp_unredirected_cutoff = (now - timedelta(days=hp_days * 2)).isoformat()
    try:
        del_redirected = await _delete_in_batches(
            "honeypot_updates",
            lambda q: q.lt("received_at", hp_cutoff).not_.is_("redirected_at", "null"),
        )
        del_unredirected = await _delete_in_batches(
            "honeypot_updates",
            lambda q: q.lt("received_at", hp_unredirected_cutoff).is_("redirected_at", "null"),
        )
        results["honeypot_updates"] = del_redirected + del_unredirected
        results["honeypot_updates_unredirected"] = del_unredirected
    except Exception as e:
        logger.debug(f"[Retention] honeypot_updates prune error: {e}")
        results["honeypot_updates_error"] = str(e)

    # 4. Engagement events (expired)
    try:
        results["engagement_events"] = await _delete_in_batches(
            "engagement_events", lambda q: q.lt("expires_at", now.isoformat())
        )
    except Exception as e:
        logger.debug(f"[Retention] engagement_events prune error: {e}")
        results["engagement_events_error"] = str(e)

    # 5. Exfiltrated messages:
    #    a) Broadcasted messages (> 30 days)
    #    b) Stale unbroadcasted/failed messages (> 60 days) to prevent infinite accumulation
    msg_stale_cutoff = (now - timedelta(days=msg_days * 2)).isoformat()
    try:
        del_broadcasted = await _delete_in_batches(
            "exfiltrated_messages",
            lambda q: q.eq("is_broadcasted", True).lt("created_at", msg_cutoff),
        )
        del_unbroadcasted = await _delete_in_batches(
            "exfiltrated_messages",
            lambda q: q.eq("is_broadcasted", False).lt("created_at", msg_stale_cutoff),
        )
        results["exfiltrated_messages"] = del_broadcasted + del_unbroadcasted
        results["exfiltrated_messages_stale_unbroadcasted"] = del_unbroadcasted
    except Exception as e:
        logger.debug(f"[Retention] exfiltrated_messages prune error: {e}")
        results["exfiltrated_messages_error"] = str(e)

    total_deleted = sum(v for v in results.values() if isinstance(v, int))
    summary_msg = f"[Retention] Cleaned up {total_deleted} rows: {results}"
    logger.info(summary_msg)

    if total_deleted > 0:
        try:
            from app.core.audit import AuditEvent, AuditLogger
            AuditLogger.log(
                "retention.cleanup_completed",
                details=results,
            )
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")

        try:
            broadcaster = get_broadcaster()
            await broadcaster.send_log(f"🧹 **Retention Cleanup**: Pruned {total_deleted} rows: {results}")
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")

    return {"status": "complete", "total_deleted": total_deleted, "details": results}
