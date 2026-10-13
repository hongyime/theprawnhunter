"""Scanner client for GitHub Gists."""

import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from ..common import *


GITHUB_SEARCH_PAUSE_KEY = "scanner:github:search:pause-until-ms"
SET_GITHUB_SEARCH_PAUSE_SCRIPT = """
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


@dataclass
class GithubGistScan:
    results: list[dict[str, Any]] = field(default_factory=list)
    completed_files: list[dict[str, str]] = field(default_factory=list)
    failed_files: list[dict[str, str]] = field(default_factory=list)
    search_error: str | None = None
    messages: list[str] = field(default_factory=list)
    gist_count: int = 0
    missing_token: bool = False


class GithubGistRequestError(RuntimeError):
    def __init__(self, message: str, *, rate_limited: bool = False):
        self.rate_limited = rate_limited
        super().__init__(message)


class GithubGistService:
    MAX_RATE_LIMIT_WAIT_SECONDS = 300
    RAW_REQUEST_GAP_SECONDS = 1.0

    def __init__(self):
        self.token = (getattr(settings, "GITHUB_TOKEN", None) or "").strip()
        self.base_url = "https://api.github.com/gists/public"

    @staticmethod
    def _redis_client():
        from app.workers.tasks.flow_tasks import redis_client
        return redis_client

    def _get_token(self) -> str:
        pool = getattr(settings, "GITHUB_TOKENS", None)
        tokens = [token.strip() for token in pool.split(",") if token.strip()] if pool else []
        if not tokens:
            return (self.token or getattr(settings, "GITHUB_TOKEN", "") or "").strip()
        if len(tokens) == 1:
            return tokens[0]
        try:
            token_index = int(self._redis_client().incr("github_token_rotation")) % len(tokens)
            return tokens[token_index]
        except Exception as error:
            logger.warning(
                "[Gist] Could not use the shared GitHub token rotation (%s); "
                "choosing from the configured token pool for this request.",
                error,
            )
            return random.choice(tokens)

    @staticmethod
    def _response_message(response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
        return (getattr(response, "text", "") or "").strip()[:300]

    @staticmethod
    def _is_rate_limited(response, message: str) -> bool:
        headers = {
            str(key).lower(): str(value)
            for key, value in (getattr(response, "headers", {}) or {}).items()
        }
        lowered = message.lower()
        return response.status_code == 429 or (
            response.status_code == 403
            and (
                headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in lowered
                or "abuse detection" in lowered
            )
        )

    @staticmethod
    def _rate_limit_wait_seconds(response) -> float:
        headers = {
            str(key).lower(): str(value)
            for key, value in (getattr(response, "headers", {}) or {}).items()
        }
        waits = []
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                try:
                    wait = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    wait = 0
            if wait > 0:
                waits.append(wait)
        if headers.get("x-ratelimit-remaining") == "0":
            try:
                wait = float(headers["x-ratelimit-reset"]) - time.time()
            except (KeyError, ValueError):
                wait = 0
            if wait > 0:
                waits.append(wait)
        return max(1.0, *waits) if waits else 60.0

    def _share_rate_limit_pause(self, wait_seconds: float, context: str) -> None:
        try:
            self._redis_client().eval(
                SET_GITHUB_SEARCH_PAUSE_SCRIPT,
                1,
                GITHUB_SEARCH_PAUSE_KEY,
                max(1000, int(wait_seconds * 1000)),
            )
        except Exception as error:
            logger.error(
                "[Gist] %s hit a GitHub rate limit, but its wait could not be shared "
                "with GitHub Search: %s",
                context,
                error,
            )

    async def _wait_for_shared_pause(self, context: str) -> None:
        try:
            pause_until_ms = int(self._redis_client().get(GITHUB_SEARCH_PAUSE_KEY) or 0)
        except Exception as error:
            raise GithubGistRequestError(
                f"could not check GitHub Search's shared rate-limit wait before {context}: {error}; "
                "this request stopped and the next scheduled scan will try again"
            ) from error
        wait_seconds = pause_until_ms / 1000 - time.time()
        if wait_seconds <= 0:
            return
        if wait_seconds > self.MAX_RATE_LIMIT_WAIT_SECONDS:
            raise GithubGistRequestError(
                f"{context} stopped because GitHub Search's shared rate-limit wait is "
                f"{wait_seconds:.0f}s; the next scheduled scan will try again",
                rate_limited=True,
            )
        logger.warning(
            "[Gist] %s is waiting %.0fs for GitHub Search's shared rate-limit pause.",
            context,
            wait_seconds,
        )
        await asyncio.sleep(wait_seconds)

    async def _get_public_gists(self, token: str) -> list[dict[str, Any]]:
        scheme = "Bearer" if token.startswith(("ghp_", "github_pat_")) else "token"
        headers = {
            "Authorization": f"{scheme} {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        params = {"per_page": 100, "page": 1}
        rate_retried = False
        async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
            while True:
                await self._wait_for_shared_pause("public Gist page 1")
                try:
                    response = await client.get(self.base_url, headers=headers, params=params)
                except (TimeoutError, httpx.RequestError) as error:
                    raise GithubGistRequestError(
                        f"public Gist page 1 could not reach GitHub: {type(error).__name__}: {error}"
                    ) from error

                message = self._response_message(response) or "no message from GitHub"
                if self._is_rate_limited(response, message):
                    wait_seconds = self._rate_limit_wait_seconds(response)
                    self._share_rate_limit_pause(wait_seconds, "Public Gist page 1")
                    if not rate_retried and wait_seconds <= self.MAX_RATE_LIMIT_WAIT_SECONDS:
                        logger.warning(
                            "[Gist] Public Gist page 1 was rate limited (HTTP %s: %s); "
                            "waiting %.0fs before one retry.",
                            response.status_code,
                            message,
                            wait_seconds,
                        )
                        await asyncio.sleep(wait_seconds)
                        rate_retried = True
                        continue
                    raise GithubGistRequestError(
                        f"public Gist page 1 was rate limited (HTTP {response.status_code}: {message}); "
                        f"the shared GitHub pause is {wait_seconds:.0f}s, so this page stops and "
                        "the next scheduled scan will try it again",
                        rate_limited=True,
                    )

                if response.status_code != 200:
                    raise GithubGistRequestError(
                        f"public Gist page 1 returned HTTP {response.status_code}: {message}"
                    )
                try:
                    items = response.json()
                except Exception as error:
                    raise GithubGistRequestError(
                        f"public Gist page 1 returned invalid JSON: {type(error).__name__}: {error}"
                    ) from error
                if not isinstance(items, list):
                    if isinstance(items, dict) and items.get("message"):
                        message = str(items["message"])
                        raise GithubGistRequestError(
                            f"public Gist page 1 returned an error inside HTTP 200: {message}"
                        )
                    raise GithubGistRequestError(
                        "public Gist page 1 returned HTTP 200 with an invalid list of Gists"
                    )
                return items

    @staticmethod
    def _clean_file_jobs(file_jobs: list[dict[str, str]]) -> list[dict[str, str]]:
        found = {}
        for job in file_jobs:
            if not isinstance(job, dict):
                continue
            raw_url = job.get("raw_url")
            if not isinstance(raw_url, str) or not raw_url.startswith("https://"):
                continue
            clean_job = {
                "gist_id": str(job.get("gist_id") or "unknown"),
                "file": str(job.get("file") or "unknown"),
                "raw_url": raw_url,
            }
            key = (clean_job["gist_id"], clean_job["file"], raw_url)
            found[key] = clean_job
        return list(found.values())

    async def search(self, retry_files: list[dict[str, str]] | None = None) -> GithubGistScan:
        retry_files = self._clean_file_jobs(retry_files or [])
        token = self._get_token()
        if not token:
            logger.warning(
                "[Gist] GitHub login token is missing or empty; public Gist search is skipped, "
                "and saved file retries are kept for the next scan."
            )
            missing = "login token is missing or empty; public Gist search was skipped"
            return GithubGistScan(
                failed_files=[{**job, "error": missing} for job in retry_files],
                missing_token=True,
            )

        scan = GithubGistScan()
        rate_limited = False
        try:
            items = await self._get_public_gists(token)
            scan.gist_count = len(items)
        except GithubGistRequestError as error:
            scan.search_error = str(error)
            rate_limited = error.rate_limited
            items = []
        except Exception as error:
            scan.search_error = f"public Gist page 1 failed: {type(error).__name__}: {error}"
            items = []

        new_files = []
        for index, gist in enumerate(items):
            if not isinstance(gist, dict):
                scan.messages.append(
                    f"Gist entry {index + 1} on public page 1 was not an object; "
                    "the next scheduled scan will read the page again."
                )
                continue
            gist_id = str(gist.get("id") or f"entry-{index + 1}")
            files = gist.get("files", {})
            if not isinstance(files, dict):
                scan.messages.append(
                    f"Gist {gist_id} had an invalid file list; the next scheduled scan "
                    "will read the public page again."
                )
                continue
            for filename, file_info in files.items():
                if not isinstance(file_info, dict):
                    scan.messages.append(
                        f"Gist {gist_id} file {filename} had invalid details; "
                        "the next scheduled scan will read the public page again."
                    )
                    continue
                raw_url = file_info.get("raw_url")
                if isinstance(raw_url, str) and raw_url.startswith("https://"):
                    new_files.append({"gist_id": gist_id, "file": str(filename), "raw_url": raw_url})

        file_jobs = self._clean_file_jobs(retry_files + new_files)
        if rate_limited:
            reason = "not fetched because the public Gist page hit a rate limit; saved for the next scan"
            scan.failed_files.extend({**job, "error": reason} for job in file_jobs)
            return scan

        if not file_jobs:
            return scan

        from app.services.scanners import TOKEN_PATTERN, _is_valid_token

        def keep_completed_file(file_job, response):
            found = TOKEN_PATTERN.findall(response.text)
            scan.results.extend(
                {
                    "token": found_token,
                    "meta": {
                        "source": "gist",
                        "gist_id": file_job["gist_id"],
                        "file": file_job["file"],
                    },
                }
                for found_token in found
                if _is_valid_token(found_token)
            )
            scan.completed_files.append(file_job)

        try:
            async with get_async_http_client(timeout=10.0) as raw_client:
                for index, file_job in enumerate(file_jobs):
                    if index:
                        await asyncio.sleep(self.RAW_REQUEST_GAP_SECONDS)
                    try:
                        response = await raw_client.get(file_job["raw_url"])
                    except Exception as error:
                        scan.failed_files.append({
                            **file_job,
                            "error": f"could not reach raw file: {type(error).__name__}: {error}",
                        })
                        continue

                    message = self._response_message(response) or "no message from GitHub"
                    if self._is_rate_limited(response, message):
                        wait_seconds = self._rate_limit_wait_seconds(response)
                        self._share_rate_limit_pause(
                            wait_seconds,
                            f"Gist {file_job['gist_id']} file {file_job['file']}",
                        )
                        if wait_seconds <= self.MAX_RATE_LIMIT_WAIT_SECONDS:
                            logger.warning(
                                "[Gist] Gist %s file %s was rate limited (HTTP %s: %s); "
                                "waiting %.0fs before one retry.",
                                file_job["gist_id"],
                                file_job["file"],
                                response.status_code,
                                message,
                                wait_seconds,
                            )
                            await asyncio.sleep(wait_seconds)
                            try:
                                response = await raw_client.get(file_job["raw_url"])
                            except Exception as error:
                                scan.failed_files.append({
                                    **file_job,
                                    "error": f"retry after rate limit failed: {type(error).__name__}: {error}",
                                })
                                continue
                            message = self._response_message(response) or "no message from GitHub"
                            if response.status_code == 200:
                                keep_completed_file(file_job, response)
                                continue
                            if self._is_rate_limited(response, message):
                                next_wait = self._rate_limit_wait_seconds(response)
                                self._share_rate_limit_pause(
                                    next_wait,
                                    f"Gist {file_job['gist_id']} file {file_job['file']}",
                                )
                                reason = (
                                    f"still rate limited after waiting (HTTP {response.status_code}: {message}); "
                                    "this file and later files are saved for the next scan"
                                )
                            else:
                                reason = (
                                    f"retry after rate limit returned HTTP {response.status_code}: {message}; "
                                    "this file failed and will be tried on the next scan"
                                )
                                scan.failed_files.append({**file_job, "error": reason})
                                continue
                        else:
                            reason = (
                                f"rate limit asks for a {wait_seconds:.0f}s wait, over the "
                                f"{self.MAX_RATE_LIMIT_WAIT_SECONDS}s limit for this run; "
                                "this file and later files are saved for the next scan"
                            )
                        scan.failed_files.extend(
                            {**pending, "error": reason}
                            for pending in file_jobs[index:]
                        )
                        break

                    if response.status_code != 200:
                        scan.failed_files.append({
                            **file_job,
                            "error": f"returned HTTP {response.status_code}: {message}",
                        })
                        continue

                    keep_completed_file(file_job, response)
        except Exception as error:
            unfinished = file_jobs[len(scan.completed_files) + len(scan.failed_files):]
            scan.failed_files.extend(
                {
                    **job,
                    "error": f"raw-file client failed: {type(error).__name__}: {error}",
                }
                for job in unfinished
            )

        return scan
