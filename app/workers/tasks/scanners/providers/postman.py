"""Scanner task implementations for postman."""

from ..common import *

@app.task(name="scanner.scan_postman", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_postman():
    return _run_sync(_scan_postman_async())

async def _scan_postman_async():
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [Postman] starting public workspace scan...")
    await _send_log_async("🔍 [Postman] scanning public collections for token leaks...")
    try:
        results = await postman_srv.search()
        if results:
            saved = await _save_credentials_async(results, "postman")
            msg = f"Postman: enqueued {saved} tokens"
        else:
            msg = "Postman: 0 matches"
        await _send_log_async(f"🏁 [Postman] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Postman] {e}", exc_info=True)
        raise
