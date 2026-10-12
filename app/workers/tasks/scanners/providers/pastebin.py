"""Scanner task implementations for pastebin."""

from ..common import *

@app.task(
    name="scanner.scan_pastebin", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_pastebin(query: str = None):
    return _run_sync(_scan_pastebin_async(query))

async def _scan_pastebin_async(query: str = None):
    return await _run_scanner(
        label="Pastebin",
        search_fn=pastebin_srv.search,
        source_name="pastebin",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
