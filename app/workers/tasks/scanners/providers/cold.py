"""Scanner task implementations for cold."""

from ..common import *

@app.task(name="scanner.retry_cold")
def retry_cold():
    return _run_sync(_retry_cold_async())

async def _retry_cold_async():
    if redis_client.get("system:paused"):
        return "System Paused"

    from datetime import datetime, timedelta

    from app.workers.tasks.flow_tasks import async_execute, enrich_credential

    logger.info("🔄 [RetryCold] Starting retry for low-viability but valid tokens...")
    await _send_log_async("🔄 [RetryCold] Starting chat discovery retry for cold valid tokens...")

    threshold = datetime.now(UTC) - timedelta(hours=6)
    threshold_str = threshold.strftime("%Y-%m-%dT%H:%M:%S")

    res = await async_execute(
        db.table("discovered_credentials")
        .select("id, meta")
        .eq("meta->>retryable", "true")
        .filter("meta->>retry_reason", "in", '("low_viability","no_chat_evidence")')
        .lt("meta->>last_gate_at", threshold_str)
    )

    retried = 0
    for row in res.data:
        cred_id = row["id"]
        reason = (row.get("meta") or {}).get("retry_reason", "")
        try:
            logger.info(f"    🔁 [RetryCold] Retrying {cred_id} (reason: {reason})")
            # Fetch current meta first then merge — avoid overwriting topic_id, bot info, etc.
            cur = await async_execute(db.table("discovered_credentials").select("meta").eq("id", cred_id).single())
            existing_meta = (cur.data or {}).get("meta") or {}
            existing_meta["retryable"] = False
            existing_meta["last_retry_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            await async_execute(
                db.table("discovered_credentials")
                .update({"meta": existing_meta})
                .eq("id", cred_id)
            )
            enrich_credential.delay(cred_id)
            retried += 1
        except Exception as e:
            logger.error(f"    ❌ [RetryCold] Failed to retry {cred_id}: {e}")

    result_msg = f"RetryCold finished. Retried {retried} tokens."
    await _send_log_async(f"🏁 [RetryCold] Finished. Retried {retried} tokens.")
    return result_msg
