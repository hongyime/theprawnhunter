"""Scanner task implementations for GitHub Actions CI logs."""

from ..common import *

@app.task(
    name="scanner.scan_ci_logs", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_ci_logs():
    return _run_sync(_scan_ci_logs_async())

async def _scan_ci_logs_async():
    from app.services.scanners_extension import CiLogsService

    ci_logs_srv = CiLogsService()

    return await _run_scanner(
        label="CI Logs",
        search_fn=ci_logs_srv.search,
        source_name="ci_logs",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
