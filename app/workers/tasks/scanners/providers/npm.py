"""Scanner task implementations for the npm registry."""

from ..common import *

@app.task(
    name="scanner.scan_npm", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_npm():
    return _run_sync(_scan_npm_async())

async def _scan_npm_async():
    from app.services.scanners_extension import NpmService

    npm_srv = NpmService()

    return await _run_scanner(
        label="npm",
        search_fn=npm_srv.search,
        source_name="npm",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
