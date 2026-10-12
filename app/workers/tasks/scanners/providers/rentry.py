"""Scanner task implementations for rentry."""

from ..common import *

@app.task(
    name="scanner.scan_rentry", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_rentry(query: str = None):
    return _run_sync(_scan_rentry_async(query))

async def _scan_rentry_async(query: str = None):
    from app.services.scanners_extension import RentryService
    rentry_srv = RentryService()
    return await _run_scanner(
        label="Rentry",
        search_fn=rentry_srv.search,
        source_name="rentry",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
