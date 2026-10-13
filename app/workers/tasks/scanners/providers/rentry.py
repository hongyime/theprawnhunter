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

    async def save_rentry_results(results, source_name: str):
        try:
            saved = await _save_credentials_async(results, source_name)
        except Exception as error:
            rentry_srv.keep_unsaved_results(results, error)
            raise
        rentry_srv.mark_saved_results(results)
        return saved

    return await _run_scanner(
        label="Rentry",
        search_fn=rentry_srv.search,
        source_name="rentry",
        save_fn=save_rentry_results,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
