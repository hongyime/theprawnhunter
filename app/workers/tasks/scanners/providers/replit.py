"""Scanner task implementations for replit."""

from ..common import *

@app.task(name="scanner.scan_replit", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_replit():
    return _run_sync(_scan_replit_async())

async def _scan_replit_async():
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [Replit] starting public repl scan...")
    await _send_log_async("🔍 [Replit] scanning public repls for token leaks...")
    try:
        results = await replit_srv.search()
        if results:
            saved = await _save_credentials_async(results, "replit")
            msg = f"Replit: enqueued {saved} tokens"
        else:
            msg = "Replit: 0 matches"
        await _send_log_async(f"🏁 [Replit] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Replit] {e}", exc_info=True)
        raise
