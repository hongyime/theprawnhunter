"""Scanner task implementations for gitlab."""

from ..common import *

@app.task(name="scanner.scan_gitlab", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_gitlab(query: str = None):
    return _run_sync(_scan_gitlab_async(query))

async def _scan_gitlab_async(query: str = None):
    return await _run_scanner(
        label="GitLab",
        search_fn=gitlab_srv.search,
        source_name="gitlab",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
        cooldown_key="cooldown:scanner:gitlab_api_broken",
        cooldown_env="GITLAB_BROKEN_COOLDOWN_SECS",
    )
