"""Scanner task implementations for sourcegraph."""

from ..common import *

@app.task(name="scanner.scan_sourcegraph", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_sourcegraph(query: str = None):
    return _run_sync(_scan_sourcegraph_async(query))

async def _scan_sourcegraph_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [Sourcegraph] starting...")
    await _send_log_async("🔍 [Sourcegraph] streaming search across public repos...")
    try:
        results = await sourcegraph_srv.search(query)
        if results:
            saved = await _save_credentials_async(results, "sourcegraph")
            msg = f"Sourcegraph: enqueued {saved} tokens"
        else:
            msg = "Sourcegraph: 0 matches"
        await _send_log_async(f"🏁 [Sourcegraph] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Sourcegraph] {e}", exc_info=True)
        raise
