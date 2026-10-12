"""Scanner task implementations for commoncrawl."""

from ..common import *

@app.task(name="scanner.scan_commoncrawl", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_commoncrawl():
    return _run_sync(_scan_commoncrawl_async())

async def _scan_commoncrawl_async():
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [CommonCrawl] starting...")
    await _send_log_async("🔍 [CommonCrawl] querying latest crawl index...")
    try:
        limit = int(os.getenv("COMMONCRAWL_LIMIT", 500))
        results = await commoncrawl_srv.search(limit=limit)
        if results:
            saved = await _save_credentials_async(results, "commoncrawl")
            msg = f"CommonCrawl: enqueued {saved} tokens"
        else:
            msg = "CommonCrawl: 0 matches"
        await _send_log_async(f"🏁 [CommonCrawl] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[CommonCrawl] {e}", exc_info=True)
        raise
