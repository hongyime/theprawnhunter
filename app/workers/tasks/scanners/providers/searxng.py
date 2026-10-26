"""Scanner task implementations for the SearXNG meta-search source."""

from ..common import *


@app.task(
    name="scanner.scan_searxng", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_searxng(query: str = None):
    return _run_sync(_scan_searxng_async(query))


async def _scan_searxng_async(query: str = None):
    from app.infrastructure.scanners.providers.searxng import SearxngService

    service = SearxngService()
    return await _run_scanner(
        label="SearXNG",
        search_fn=service.search,
        source_name="searxng",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
        queries=[query] if query else SearxngService.QUERIES,
    )
