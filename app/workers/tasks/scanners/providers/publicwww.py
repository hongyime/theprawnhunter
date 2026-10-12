"""Scanner task implementations for publicwww."""

from ..common import *

@app.task(
    name="scanner.scan_publicwww", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_publicwww(query: str = None):
    return _run_sync(_scan_publicwww_async(query))

async def _scan_publicwww_async(query: str = None):
    return await _run_scanner(
        label="PublicWWW",
        search_fn=publicwww_srv.search,
        source_name="publicwww",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
