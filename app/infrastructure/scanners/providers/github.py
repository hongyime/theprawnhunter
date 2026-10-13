"""Scanner client for Github."""

import math
import os
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from ..common import *


GITHUB_CODE_SEARCH_GAP_MS = 6100
GITHUB_CODE_SEARCH_LAST_REQUEST_KEY = "scanner:github:search:last-request-ms"
GITHUB_CODE_SEARCH_PAUSE_KEY = "scanner:github:search:pause-until-ms"

_CLAIM_CODE_SEARCH_SLOT_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local last = tonumber(redis.call('GET', KEYS[1]) or '0')
local pause_until = tonumber(redis.call('GET', KEYS[2]) or '0')
local next_at = math.max(last + tonumber(ARGV[1]), pause_until)
if next_at > now then
    return next_at - now
end
redis.call('SET', KEYS[1], now)
return 0
"""

_SET_CODE_SEARCH_PAUSE_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local requested = now + tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if requested > current then
    redis.call('SET', KEYS[1], requested)
    return requested
end
return current
"""


class GithubSearchError(RuntimeError):
    """A GitHub search could not be completed."""


class GithubRateLimitError(GithubSearchError):
    """A GitHub request hit a rate limit and must wait before more requests."""

    def __init__(self, query: str, wait_seconds: float, message: str):
        self.query = query
        self.wait_seconds = wait_seconds
        super().__init__(message)


class _GithubRateLimitResponse(Exception):
    def __init__(self, status_code: int, wait_seconds: float, message: str):
        self.status_code = status_code
        self.wait_seconds = wait_seconds
        self.message = message


class GithubService:
    REPO_PUSH_CACHE_SECONDS = 300

    def __init__(self):
        self.token = settings.GITHUB_TOKEN  # backwards-compat fallback
        self.base_url = "https://api.github.com/search/code"
        self._repo_push_cache: dict[str, tuple[float, str | None]] = {}
        self._local_search_pause_until = 0.0

    async def _wait_for_code_search_slot(self, query: str) -> None:
        """Pace code searches across all workers that share Redis."""
        from app.workers.tasks.flow_tasks import redis_client

        while True:
            local_wait = self._local_search_pause_until - time.time()
            if local_wait > 0:
                logger.warning(
                    "[GitHub] Search %r is waiting %.1fs after a rate limit.",
                    query,
                    local_wait,
                )
                await asyncio.sleep(local_wait)
                continue

            try:
                wait_ms = int(
                    redis_client.eval(
                        _CLAIM_CODE_SEARCH_SLOT_SCRIPT,
                        2,
                        GITHUB_CODE_SEARCH_LAST_REQUEST_KEY,
                        GITHUB_CODE_SEARCH_PAUSE_KEY,
                        GITHUB_CODE_SEARCH_GAP_MS,
                    )
                )
            except Exception as error:
                raise GithubSearchError(
                    f"GitHub search {query!r} could not check the shared request timer in Redis: {error}"
                ) from error

            if wait_ms <= 0:
                return

            wait_seconds = wait_ms / 1000
            logger.info(
                "[GitHub] Search %r is waiting %.1fs for its shared request slot.",
                query,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

    def _record_rate_limit(self, query: str, wait_seconds: float) -> None:
        """Pause this worker and share the pause with other workers when possible."""
        wait_ms = max(1000, int(wait_seconds * 1000))
        self._local_search_pause_until = max(
            self._local_search_pause_until,
            time.time() + wait_ms / 1000,
        )

        try:
            from app.workers.tasks.flow_tasks import redis_client

            redis_client.eval(
                _SET_CODE_SEARCH_PAUSE_SCRIPT,
                1,
                GITHUB_CODE_SEARCH_PAUSE_KEY,
                wait_ms,
            )
        except Exception as error:
            logger.error(
                "[GitHub] Search %r hit a rate limit; this worker will wait %.1fs, "
                "but Redis could not share the pause with other workers: %s.",
                query,
                wait_ms / 1000,
                error,
            )

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

    async def _get_repo_pushed_at(
        self, client, full_name: str, headers: dict, query: str
    ) -> str | None:
        cached = self._repo_push_cache.get(full_name)
        if cached and time.monotonic() - cached[0] < self.REPO_PUSH_CACHE_SECONDS:
            return cached[1]

        try:
            repo_url = f"https://api.github.com/repos/{quote(full_name, safe='/')}"
            response = await client.get(repo_url, headers=headers)
            self._raise_for_github_response(response, query, repo_url)
            pushed_at = response.json().get("pushed_at")
        except _GithubRateLimitResponse as error:
            raise GithubRateLimitError(
                query,
                error.wait_seconds,
                f"GitHub search {query!r} was rate limited while checking {full_name}: {error.message}",
            ) from error
        except Exception as error:
            raise GithubSearchError(
                f"GitHub search {query!r} could not check the pushed date for {full_name}: {error}"
            ) from error

        self._repo_push_cache[full_name] = (time.monotonic(), pushed_at)
        return pushed_at

    async def _filter_recent_items(
        self, items: list[dict], headers: dict, cutoff: datetime, query: str
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
            semaphore = asyncio.Semaphore(3)
            stop_repo_checks = asyncio.Event()
            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                async def check_repo(full_name):
                    async with semaphore:
                        if stop_repo_checks.is_set():
                            return full_name, None
                        try:
                            pushed_at = await self._get_repo_pushed_at(
                                client, full_name, headers, query
                            )
                        except GithubRateLimitError:
                            stop_repo_checks.set()
                            raise
                        except Exception:
                            stop_repo_checks.set()
                            raise
                        return full_name, pushed_at

                repo_results = await asyncio.gather(
                    *(check_repo(full_name) for full_name in repo_names_to_check),
                    return_exceptions=True,
                )

            for result in repo_results:
                if isinstance(result, GithubRateLimitError):
                    raise result
                if isinstance(result, Exception):
                    raise GithubSearchError(
                        f"GitHub search {query!r} could not check repository push dates: {result}"
                    ) from result
                full_name, pushed_at = result
                if pushed_at is not None:
                    pushed_at_by_repo[full_name] = pushed_at

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
            return (self.token or "").strip()
        tokens = [t.strip() for t in pool.split(",") if t.strip()]
        if not tokens:
            return (self.token or "").strip()
        if len(tokens) == 1:
            return tokens[0]
        try:
            from app.workers.tasks.flow_tasks import redis_client
            idx = int(redis_client.incr("github_token_rotation")) % len(tokens)
            return tokens[idx]
        except Exception:
            import random as _r
            return _r.choice(tokens)

    def has_token(self) -> bool:
        """Check token settings without rotating the token pool."""
        pool = getattr(settings, "GITHUB_TOKENS", None)
        if pool and any(token.strip() for token in pool.split(",")):
            return True
        return bool((self.token or "").strip())

    @staticmethod
    def _response_message(response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
        return str(getattr(response, "text", "") or "").strip()

    @staticmethod
    def _rate_limit_wait_seconds(response) -> float:
        response_headers = getattr(response, "headers", {}) or {}
        headers = {str(key).lower(): str(value) for key, value in response_headers.items()}
        waits = []

        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                retry_seconds = float(retry_after)
            except ValueError:
                try:
                    retry_seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    retry_seconds = 0.0
            if math.isfinite(retry_seconds) and retry_seconds > 0:
                waits.append(retry_seconds)

        if headers.get("x-ratelimit-remaining") == "0":
            try:
                reset_wait = float(headers["x-ratelimit-reset"]) - time.time()
            except (KeyError, ValueError):
                reset_wait = 0.0
            if math.isfinite(reset_wait) and reset_wait > 0:
                waits.append(reset_wait)

        return max(1.0, *waits) if waits else 60.0

    def _raise_for_github_response(self, response, query: str, endpoint: str) -> None:
        status_code = response.status_code
        message = self._response_message(response) or "no error message from GitHub"
        response_headers = getattr(response, "headers", {}) or {}
        headers = {str(key).lower(): str(value) for key, value in response_headers.items()}
        is_rate_limited = (
            status_code == 429
            or (status_code == 403 and (
                headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in message.lower()
            ))
        )
        if is_rate_limited:
            wait_seconds = self._rate_limit_wait_seconds(response)
            self._record_rate_limit(query, wait_seconds)
            raise _GithubRateLimitResponse(
                status_code,
                wait_seconds,
                f"HTTP {status_code} at {endpoint}: {message}",
            )
        response.raise_for_status()

    async def _request_code_items(self, query: str, headers: dict, params: dict) -> list[dict]:
        try:
            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                response = await client.get(self.base_url, headers=headers, params=params)
            self._raise_for_github_response(response, query, self.base_url)
        except _GithubRateLimitResponse:
            raise
        except httpx.HTTPStatusError as error:
            message = self._response_message(error.response) or str(error)
            raise GithubSearchError(
                f"GitHub search {query!r} returned HTTP "
                f"{error.response.status_code}: {message}"
            ) from error
        except (TimeoutError, httpx.RequestError) as error:
            raise GithubSearchError(
                f"GitHub search {query!r} could not reach GitHub: "
                f"{type(error).__name__}: {error}"
            ) from error

        payload = response.json()
        if payload.get("incomplete_results"):
            raise GithubSearchError(
                f"GitHub search {query!r} returned incomplete results; "
                "the search is saved for the next scan."
            )
        return payload.get("items", [])

    async def search(
        self, query: str, pushed_since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Search code, optionally limiting matches to recently pushed repositories."""
        token = self._get_token()
        if not token:
            raise GithubSearchError(
                f"GitHub login token is missing for search {query!r} "
                "(set GITHUB_TOKEN or GITHUB_TOKENS)"
            )

        try:
            # Support both classic (ghp_) and fine-grained (github_pat_) tokens
            auth_scheme = "Bearer" if token.startswith(("ghp_", "github_pat_")) else "token"
            headers = {
                'Authorization': f'{auth_scheme} {token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            params = {'q': query, 'per_page': 100, 'sort': 'indexed', 'order': 'desc'}

            try:
                await self._wait_for_code_search_slot(query)
                items = await self._request_code_items(query, headers, params)
            except _GithubRateLimitResponse as error:
                raise GithubRateLimitError(
                    query,
                    error.wait_seconds,
                    f"GitHub search {query!r} was rate limited: {error.message}",
                ) from error
            if pushed_since is not None:
                items = await self._filter_recent_items(items, headers, pushed_since, query)

            if not items:
                return []

            # Parallel Raw Fetching
            results = []
            async with httpx.AsyncClient(verify=False, timeout=10.0) as raw_client:
                tasks = []
                sem = asyncio.Semaphore(3)
                stop_raw_fetches = asyncio.Event()

                async def fetch_raw(item):
                    async with sem:
                        if stop_raw_fetches.is_set():
                            return []
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
                                logger.warning(
                                    "[GitHub] Search %r found %s, but the file is gone (HTTP 404); "
                                    "skipping this stale file and checking the remaining matches.",
                                    query,
                                    item.get('path') or html_url,
                                )
                                return []
                            self._raise_for_github_response(raw_res, query, raw_url)
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
                        except _GithubRateLimitResponse as error:
                            stop_raw_fetches.set()
                            raise GithubRateLimitError(
                                query,
                                error.wait_seconds,
                                f"GitHub search {query!r} was rate limited while fetching "
                                f"{item.get('path') or html_url}: {error.message}",
                            ) from error
                        except GithubRateLimitError:
                            raise
                        except Exception as error:
                            stop_raw_fetches.set()
                            raise GithubSearchError(
                                f"GitHub search {query!r} could not fetch "
                                f"{item.get('path') or html_url}: {error}"
                            ) from error

                for item in items:
                    tasks.append(fetch_raw(item))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)

                for batch in scan_results:
                    if isinstance(batch, GithubRateLimitError):
                        raise batch
                    if isinstance(batch, Exception):
                        raise GithubSearchError(
                            f"GitHub search {query!r} could not fetch a matched file: {batch}"
                        ) from batch
                    if batch and isinstance(batch, list):
                        results.extend(batch)
            return results
        except GithubSearchError:
            raise
        except httpx.TimeoutException as error:
            raise GithubSearchError(
                f"GitHub search {query!r} timed out: {error}"
            ) from error
        except httpx.RequestError as error:
            raise GithubSearchError(
                f"GitHub search {query!r} could not reach GitHub: {error}"
            ) from error
        except httpx.HTTPStatusError as error:
            message = self._response_message(error.response) or str(error)
            raise GithubSearchError(
                f"GitHub search {query!r} returned HTTP {error.response.status_code}: {message}"
            ) from error
        except Exception as error:
            raise GithubSearchError(
                f"GitHub search {query!r} failed: {type(error).__name__}: {error}"
            ) from error
