"""Scanner task implementations for github gist."""

from ..common import *

@app.task(name="scanner.scan_gist", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_gist(query: str = None):
    return _run_sync(_scan_gist_async(query))

async def _scan_gist_async(query: str = None):
    return await _run_scanner(
        label="Gist",
        search_fn=gist_srv.search,
        source_name="gist",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
