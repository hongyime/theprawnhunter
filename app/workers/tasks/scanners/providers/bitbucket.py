"""Scanner task implementations for bitbucket."""

from ..common import *

@app.task(
    name="scanner.scan_bitbucket", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_bitbucket(query: str = None):
    return _run_sync(_scan_bitbucket_async(query))

async def _scan_bitbucket_async(query: str = None):
    return await _run_scanner(
        label="Bitbucket",
        search_fn=bitbucket_srv.search,
        source_name="bitbucket",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
