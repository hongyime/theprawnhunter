"""Scanner task implementations for wayback."""

from ..common import *

@app.task(name="scanner.scan_wayback", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_wayback(query: str = None):
    """
    Wayback Machine historical URL scanner.
    Free, no API key, no cooldown gating needed (CDX is free + permissive).
    """
    return _run_sync(_scan_wayback_async(query))

async def _scan_wayback_async(query: str = None):
    try:
        if redis_client.get("system:paused"):
            return "System Paused"
    except Exception as error:
        logger.warning(
            "[Wayback] Could not check the pause flag (%s); continuing with this scan.", error,
        )

    logger.info("🔍 [Wayback] Starting historical URL scan...")
    try:
        query_pattern = query or "api.telegram.org"
        try:
            limit = int(os.getenv("WAYBACK_LIMIT", 500))
        except (TypeError, ValueError) as error:
            limit = 500
            logger.error(
                "[Wayback] Search %r has an invalid WAYBACK_LIMIT (%s); using 500 and continuing.",
                query_pattern, error,
            )

        try:
            await _send_log_async("🔍 [Wayback] Scanning Internet Archive for token leaks...")
        except Exception as error:
            logger.warning("[Wayback] Could not send the start status (%s); continuing with the scan.", error)
        batch = await wayback_srv.search(
            query_pattern=query_pattern,
            limit=limit,
            return_batch=True,
        )

        if batch.matches:
            saved = await _save_credentials_async(batch.matches, "wayback")
        else:
            saved = 0

        # Keep snapshots pending until token enqueueing has completed. If that
        # step raises, Celery retries the task and the next scan reads them again.
        wayback_srv.mark_completed(batch.completed_snapshot_ids, batch.snapshot_urls)

        if batch.failures:
            msg = (
                f"Wayback scan finished with {batch.failures} issue(s). "
                f"Unfinished searches or snapshots will be tried next scan. Enqueued {saved} tokens."
            )
        elif saved:
            msg = f"Wayback scan finished. Enqueued {saved} tokens for validation."
        else:
            msg = "Wayback scan finished. 0 matches."

        try:
            await _send_log_async(f"🏁 [Wayback] {msg}")
        except Exception as error:
            logger.warning("[Wayback] Could not send the finish status (%s); scan results are handled.", error)
        return msg
    except Exception as e:
        logger.error(
            "[Wayback] Search %r failed before completion (%s). "
            "Celery will retry this task.", query or "api.telegram.org", e,
            exc_info=True,
        )
        raise
