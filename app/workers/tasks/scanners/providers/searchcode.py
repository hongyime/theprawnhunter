"""Scanner task implementations for searchcode."""

from ..common import *

@app.task(name="scanner.scan_searchcode", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_searchcode(query: str = None):
    return _run_sync(_scan_searchcode_async(query))

async def _scan_searchcode_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [Searchcode] starting public code search...")
    await _send_log_async("🔍 [Searchcode] scanning searchcode.com for token leaks...")
    try:
        results = await searchcode_srv.search(query)
        if results:
            saved = await _save_credentials_async(results, "searchcode")
            msg = f"Searchcode: enqueued {saved} tokens"
        else:
            msg = "Searchcode: 0 matches"
        await _send_log_async(f"🏁 [Searchcode] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Searchcode] {e}", exc_info=True)
        raise
