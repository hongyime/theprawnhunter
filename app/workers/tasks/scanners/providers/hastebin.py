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

    async def _save_hastebin_results(results, source_name: str):
        paste_ids = sorted(
            paste_id
            for paste_id in {
                item.get("meta", {}).get("paste_id")
                for item in results
                if isinstance(item, dict) and isinstance(item.get("meta"), dict)
            }
            if paste_id
        )
        try:
            saved = await _save_credentials_async(results, source_name)
        except Exception as error:
            logger.error(
                "[Hastebin] Could not save results from paste(s) %s: %s. They remain pending for the next scan.",
                ", ".join(paste_ids) or "unknown",
                error,
            )
            raise
        hastebin_srv.mark_saved(results)
        return saved

    return await _run_scanner(
        label="Hastebin",
        search_fn=hastebin_srv.search,
        source_name="hastebin",
        save_fn=_save_hastebin_results,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
