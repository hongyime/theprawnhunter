"""Scanner task implementations for the PyPI package index."""

from ..common import *

@app.task(
    name="scanner.scan_pypi", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_pypi():
    return _run_sync(_scan_pypi_async())

async def _scan_pypi_async():
    from app.services.scanners_extension import PypiService

    pypi_srv = PypiService()

    return await _run_scanner(
        label="PyPI",
        search_fn=pypi_srv.search,
        source_name="pypi",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
