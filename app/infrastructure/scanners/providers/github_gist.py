"""Scanner client for GithubGist."""

from ..common import *

class GithubGistService:
    def __init__(self):
        pool = getattr(settings, "GITHUB_TOKENS", None)
        if pool:
            self.token = pool.split(",")[0].strip()
        else:
            self.token = settings.GITHUB_TOKEN
        self.base_url = "https://api.github.com/gists/public"

    async def search(self) -> list[dict[str, Any]]:
        # Gists public endpoint shows recently created gists.
        # We fetch the recent 100 and look for tokens.
        if not self.token:
            logger.warning("    [Gist] Missing GITHUB_TOKEN")
            return []
        try:
            auth_scheme = "Bearer" if self.token.startswith(("ghp_", "github_pat_")) else "token"
            headers = {
                'Authorization': f'{auth_scheme} {self.token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            params = {"per_page": 100}

            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                res = await client.get(self.base_url, headers=headers, params=params)
                res.raise_for_status()
                items = res.json()

            results = []
            async with httpx.AsyncClient(verify=False, timeout=10.0) as raw_client:
                tasks = []
                sem = asyncio.Semaphore(10)

                async def process_gist(gist):
                    from app.services.scanners import TOKEN_PATTERN, _is_valid_token
                    async with sem:
                        local_res = []
                        files = gist.get("files", {})
                        for filename, file_data in files.items():
                            raw_url = file_data.get("raw_url")
                            if raw_url:
                                try:
                                    raw_res = await raw_client.get(raw_url)
                                    content = raw_res.text
                                    found = TOKEN_PATTERN.findall(content)
                                    for t in found:
                                        if not _is_valid_token(t):
                                            continue
                                        local_res.append({
                                            "token": t,
                                            "meta": {"source": "gist", "gist_id": gist.get("id"), "file": filename}
                                        })
                                except Exception as _swallowed:
                                    logger.debug(f"[suppressed] {_swallowed}")
                        return local_res

                for item in items:
                    tasks.append(process_gist(item))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for batch in scan_results:
                    if batch and isinstance(batch, list):
                        results.extend(batch)

            return results

        except Exception as e:
            logger.error(f"    [Gist] Error: {e}")
            return []
