"""Scanner client for Github."""

import os
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from ..common import *


class GithubService:
    REPO_PUSH_CACHE_SECONDS = 300

    def __init__(self):
        self.token = settings.GITHUB_TOKEN  # backwards-compat fallback
        self.base_url = "https://api.github.com/search/code"
        self._repo_push_cache: dict[str, tuple[float, str | None]] = {}

    def recent_push_cutoff(self, now: datetime | None = None) -> datetime:
        """Return the rolling cutoff; GITHUB_PUSHED_WITHIN_DAYS defaults to 30."""
        try:
            days = int(os.getenv("GITHUB_PUSHED_WITHIN_DAYS", "30"))
        except ValueError:
            days = 30
        days = max(1, days)

        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        return current_time - timedelta(days=days)

    @staticmethod
    def _is_recent_push(pushed_at: str | None, cutoff: datetime) -> bool:
        if not isinstance(pushed_at, str) or not pushed_at:
            return False
        try:
            pushed_time = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if pushed_time.tzinfo is None:
            pushed_time = pushed_time.replace(tzinfo=UTC)
        return pushed_time.astimezone(UTC) >= cutoff

    async def _get_repo_pushed_at(self, client, full_name: str, headers: dict) -> str | None:
        cached = self._repo_push_cache.get(full_name)
        if cached and time.monotonic() - cached[0] < self.REPO_PUSH_CACHE_SECONDS:
            return cached[1]

        try:
            repo_url = f"https://api.github.com/repos/{quote(full_name, safe='/')}"
            response = await client.get(repo_url, headers=headers)
            response.raise_for_status()
            pushed_at = response.json().get("pushed_at")
        except Exception as error:
            logger.debug("[GitHub] Could not check pushed date for %s: %s", full_name, error)
            return None

        self._repo_push_cache[full_name] = (time.monotonic(), pushed_at)
        return pushed_at

    async def _filter_recent_items(
        self, items: list[dict], headers: dict, cutoff: datetime
    ) -> list[dict]:
        """Keep code matches from repositories pushed during the rolling date window."""
        cutoff = (
            cutoff.replace(tzinfo=UTC)
            if cutoff.tzinfo is None
            else cutoff.astimezone(UTC)
        )

        pushed_at_by_repo = {}
        repo_names_to_check = set()

        for item in items:
            repository = item.get("repository", {})
            full_name = repository.get("full_name")
            if not full_name:
                continue

            pushed_at = repository.get("pushed_at")
            if pushed_at:
                pushed_at_by_repo[full_name] = pushed_at
                self._repo_push_cache[full_name] = (time.monotonic(), pushed_at)
            else:
                repo_names_to_check.add(full_name)

        if repo_names_to_check:
            semaphore = asyncio.Semaphore(10)
            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                async def check_repo(full_name):
                    async with semaphore:
                        return full_name, await self._get_repo_pushed_at(client, full_name, headers)

                repo_dates = await asyncio.gather(
                    *(check_repo(full_name) for full_name in repo_names_to_check)
                )
            pushed_at_by_repo.update(repo_dates)

        return [
            item
            for item in items
            if self._is_recent_push(
                pushed_at_by_repo.get(item.get("repository", {}).get("full_name")),
                cutoff,
            )
        ]

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

    async def search(
        self, query: str, pushed_since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Search code, optionally limiting matches to recently pushed repositories."""
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
            items = items or []
            if pushed_since is not None:
                items = await self._filter_recent_items(items, headers, pushed_since)

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
