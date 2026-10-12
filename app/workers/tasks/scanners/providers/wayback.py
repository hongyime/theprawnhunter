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
    if redis_client.get("system:paused"):
        return "System Paused"

    logger.info("🔍 [Wayback] Starting historical URL scan...")
    await _send_log_async("🔍 [Wayback] Scanning Internet Archive for token leaks...")

    try:
        limit = int(os.getenv("WAYBACK_LIMIT", 500))
        results = await wayback_srv.search(
            query_pattern=query or "api.telegram.org",
            limit=limit,
        )

        if results:
            saved = await _save_credentials_async(results, "wayback")
            msg = f"Wayback scan finished. Enqueued {saved} tokens for validation."
        else:
            msg = "Wayback scan finished. 0 matches."

        await _send_log_async(f"🏁 [Wayback] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Wayback] Error: {e}", exc_info=True)
        raise
