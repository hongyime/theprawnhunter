"""Scanner client for Gitlab."""

from app.domain.tokens import sweep_text_for_tokens

from ..common import *


class GitlabService:
    def __init__(self):
        self.token = settings.GITLAB_TOKEN
        self.base_url = "https://gitlab.com/api/v4/search"

    async def search(self, query: str = "api.telegram.org/bot") -> list[dict[str, Any]]:
        if not self.token:
            logger.warning("    [GitLab] Missing GITLAB_TOKEN")
            return []

        try:
            headers = {"PRIVATE-TOKEN": self.token}
            params = {"scope": "blobs", "search": query}

            async def do_gitlab_search():
                async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                    res = await client.get(self.base_url, headers=headers, params=params)
                    res.raise_for_status()
                    return res.json()

            items = await retry_with_backoff(do_gitlab_search)
            if not items:
                items = []

            # GitLab blobs api returns project_id and filename.
            # We must fetch the raw blob or the file content via projects API.
            # Due to GitLab API structures, getting raw content is complex in search scopes.
            # Instead, we will simulate scraping the blob URL directly or doing a shallow parse of the match data.
            results = []

            async with httpx.AsyncClient(verify=False, timeout=10.0) as raw_client:
                tasks = []
                sem = asyncio.Semaphore(10)

                async def fetch_raw(item):
                    async with sem:
                        project_id = item.get("project_id")
                        filename = item.get("filename")
                        ref = item.get("ref", "master")

                        raw_url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/files/{filename}/raw?ref={ref}"
                        try:
                            raw_res = await raw_client.get(raw_url, headers=headers)
                            content = raw_res.text
                            if not isinstance(content, str) or not content:
                                return []
                            return [
                                {
                                    "token": hit["token"],
                                    "meta": {"source": "gitlab", "project_id": project_id, "file": filename},
                                }
                                for hit in sweep_text_for_tokens(content, "gitlab")
                            ]
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                            return []

                for item in items:
                    tasks.append(fetch_raw(item))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for batch in scan_results:
                    if batch and isinstance(batch, list):
                        results.extend(batch)

            return results

        except Exception as e:
            logger.error(f"    [GitLab] Error: {e}")
            return []
