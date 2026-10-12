"""Scanner task implementations for grepapp."""

from ..common import *

@app.task(
    name="scanner.scan_grepapp", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_grepapp(query: str = None):
    return _run_sync(_scan_grepapp_async(query))

async def _scan_grepapp_async(query: str = None):
    return await _run_scanner(
        label="GrepApp",
        search_fn=grepapp_srv.search,
        source_name="grepapp",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
