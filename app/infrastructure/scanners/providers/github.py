"""Scanner client for Github."""

from ..common import *

class GithubService:
    def __init__(self):
        self.token = settings.GITHUB_TOKEN  # backwards-compat fallback
        self.base_url = "https://api.github.com/search/code"

    def _get_token(self) -> str:
        """
        Round-robin token selection from GITHUB_TOKENS env var (comma-separated).
        Falls back to single GITHUB_TOKEN if pool not configured.

        Uses Redis INCR for distributed round-robin so multiple worker
        processes share the rotation evenly. If Redis is unreachable, falls
        back to random.choice.
        """
        pool = getattr(settings, "GITHUB_TOKENS", None)
        if not pool:
            return self.token or ""
        tokens = [t.strip() for t in pool.split(",") if t.strip()]
        if not tokens:
            return self.token or ""
        if len(tokens) == 1:
            return tokens[0]
        try:
            from app.workers.tasks.flow_tasks import redis_client
            idx = int(redis_client.incr("github_token_rotation")) % len(tokens)
            return tokens[idx]
        except Exception:
            import random as _r
            return _r.choice(tokens)

    async def search(self, query: str) -> list[dict[str, Any]]:
        token = self._get_token()
        if not token:
            logger.warning("GitHub Token missing (set GITHUB_TOKEN or GITHUB_TOKENS)")
            return []

        try:
            # Support both classic (ghp_) and fine-grained (github_pat_) tokens
            auth_scheme = "Bearer" if token.startswith(("ghp_", "github_pat_")) else "token"
            headers = {
                'Authorization': f'{auth_scheme} {token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            params = {'q': query, 'per_page': 100, 'sort': 'indexed', 'order': 'desc'}

            async def do_github():
                async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                    res = await client.get(self.base_url, headers=headers, params=params)
                    if res.status_code in [403, 429]:
                        # Check Rate Limit sleep
                        raise httpx.HTTPStatusError("Rate Limit", request=res.request, response=res)
                    res.raise_for_status()
                    return res.json().get('items', [])

            items = await retry_with_backoff(do_github, max_retries=2)
            if not items:
                items = []

            # Parallel Raw Fetching
            results = []
            async with httpx.AsyncClient(verify=False, timeout=10.0) as raw_client:
                tasks = []
                sem = asyncio.Semaphore(10) # GitHub raw is sensitive to speed?

                async def fetch_raw(item):
                    async with sem:
                        html_url = item.get('html_url', '')
                        # Convert github.com blob URL to raw.githubusercontent.com
                        raw_url = (
                            html_url
                            .replace('https://github.com', 'https://raw.githubusercontent.com')
                            .replace('/blob/', '/')
                        )
                        # Fallback: use the API to get raw content if URL conversion looks wrong
                        if '/blob/' not in html_url and 'raw.githubusercontent.com' not in raw_url:
                            # Try the GitHub contents API instead
                            repo = item.get('repository', {}).get('full_name', '')
                            path = item.get('path', '')
                            ref = item.get('sha', 'HEAD')
                            raw_url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"

                        try:
                            # Include auth to avoid 60/hr unauthenticated rate limit.
                            # Use the same rotated token as the search call so each
                            # request hits a different bucket — raw.githubusercontent
                            # has its own per-token rate limit separate from search.
                            raw_res = await raw_client.get(raw_url, headers={
                                'Authorization': f'{auth_scheme} {token}'
                            })
                            if raw_res.status_code == 404:
                                return []
                            content = raw_res.text
                            found = TOKEN_PATTERN.findall(content)
                            local_res = []
                            source_meta = {
                                "source": "github",
                                "repository": item.get('repository', {}).get('full_name'),
                                "repo": item.get('repository', {}).get('full_name'),
                                "path": item.get('path'),
                                "file_url": item.get('html_url'),
                            }
                            for t in found:
                                if not _is_valid_token(t):
                                    continue
                                meta = dict(source_meta)
                                infrastructure_context = extract_infrastructure_context(
                                    content,
                                    source_meta,
                                    token=t,
                                )
                                if infrastructure_context:
                                    meta["infrastructure_context"] = infrastructure_context
                                local_res.append({
                                    "token": t,
                                    "meta": meta
                                })
                            return local_res
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
        except httpx.TimeoutException:
            logger.warning("    [GitHub] Error: Request Timed Out")
            return []
        except httpx.RequestError as e:
            logger.error(f"    [GitHub] Network Error: {e}")
            return []
        except Exception as e:
            logger.error(f"    [GitHub] Error: {e}")
            return []
