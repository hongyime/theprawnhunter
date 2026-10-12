"""Scanner task implementations for hastebin."""

from ..common import *

@app.task(
    name="scanner.scan_hastebin", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_hastebin(query: str = None):
    return _run_sync(_scan_hastebin_async(query))

async def _scan_hastebin_async(query: str = None):
    from app.services.scanners_extension import HastebinService
    hastebin_srv = HastebinService()
    return await _run_scanner(
        label="Hastebin",
        search_fn=hastebin_srv.search,
        source_name="hastebin",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
