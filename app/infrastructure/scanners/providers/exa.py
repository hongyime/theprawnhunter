"""Scanner client for Exa."""

from email.utils import parsedate_to_datetime
from datetime import UTC, datetime

from ..common import *


MAX_RESULTS = 25
MAX_TEXT_CHARACTERS = 10000
MAX_ATTEMPTS = 4
MAX_RATE_WAIT_SECONDS = 60


class ExaSearchFailure(Exception):
    """An Exa search failed and should be tried again later."""


class ExaRateLimitFailure(ExaSearchFailure):
    """Exa asked this scan to slow down."""


class ExaKeyFailure(ExaSearchFailure):
    """No configured Exa key could complete a search."""


class ExaQuotaFailure(ExaSearchFailure):
    """Exa reports that the account has no search quota left."""


class _ExaKeyRejected(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class _ExaRateLimited(Exception):
    def __init__(self, retry_after: str | None, detail: str):
        self.retry_after = retry_after
        self.detail = detail
        super().__init__(detail)


class _ExaServerFailure(Exception):
    pass


class ExaService:
    """
    Paste-site crawler using Exa neural search API.
    Replaces SerperService — Exa returns full page content directly,
    eliminating the second HTTP fetch pass that Serper required.

    Scopes searches to known paste/code-sharing domains and extracts
    Telegram bot tokens from returned content inline.
    """

    PASTE_DOMAINS = [
        "pastebin.com",
        "hastebin.com",
        "rentry.co",
        "ghostbin.com",
        "paste.ee",
        "controlc.com",
    ]

    def __init__(self):
        self.base_url = "https://api.exa.ai/search"

    def _configured_keys(self) -> list[str]:
        return [
            key.strip()
            for key in (
                settings.EXA_API_KEY,
                getattr(settings, "EXA_API_KEY_2", None),
                getattr(settings, "EXA_API_KEY_3", None),
            )
            if isinstance(key, str) and key.strip()
        ]

    def _get_api_key(self) -> str | None:
        """Pick a random available Exa key from EXA_API_KEY / _2 / _3."""
        keys = self._configured_keys()
        return random.choice(keys) if keys else None

    @staticmethod
    def _error_detail(response) -> str:
        try:
            body = response.json()
        except Exception:
            body = getattr(response, "text", "")
        if isinstance(body, dict):
            body = body.get("error") or body.get("message") or body
        return str(body)[:300]

    @staticmethod
    def _retry_after_seconds(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return max(0, int(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0, int((retry_at - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                return None

    async def _request_once(
        self, query: str, api_key: str, include_domains: list[str] | None = None
    ) -> list[dict[str, Any]]:
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        payload = {
            "query": query,
            "numResults": MAX_RESULTS,
            "includeDomains": include_domains or self.PASTE_DOMAINS,
            "contents": {"text": {"maxCharacters": MAX_TEXT_CHARACTERS}},
            "type": "keyword",
        }

        async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
            response = await client.post(self.base_url, headers=headers, json=payload)

        status = response.status_code
        if status < 400:
            try:
                body = response.json()
            except Exception as error:
                raise ExaSearchFailure(f"reply was not valid JSON: {error}") from error
            if not isinstance(body, dict):
                raise ExaSearchFailure("reply was not a JSON object")
            if body.get("error"):
                raise ExaSearchFailure(f"error in HTTP 200 reply: {body['error']}")
            results = body.get("results")
            if not isinstance(results, list):
                raise ExaSearchFailure("reply did not include a results list")
            return results[:MAX_RESULTS]

        detail = self._error_detail(response)
        if status in (401, 403):
            raise _ExaKeyRejected(status, detail)
        if status == 402:
            raise ExaQuotaFailure(f"Exa quota is exhausted (HTTP 402): {detail}")
        if status == 429:
            raise _ExaRateLimited(response.headers.get("Retry-After"), detail)
        if status >= 500:
            raise _ExaServerFailure(f"HTTP {status}: {detail}")
        if status >= 400:
            raise ExaSearchFailure(f"HTTP {status}: {detail}")
        raise ExaSearchFailure(f"unexpected HTTP status {status}: {detail}")

    async def _request_with_retries(
        self, query: str, api_key: str, include_domains: list[str] | None = None
    ) -> list[dict[str, Any]]:
        for attempt in range(MAX_ATTEMPTS):
            try:
                return await self._request_once(query, api_key, include_domains)
            except _ExaRateLimited as error:
                retry_after = self._retry_after_seconds(error.retry_after)
                wait_seconds = retry_after if retry_after is not None else 2 ** (attempt + 1)
                if wait_seconds > MAX_RATE_WAIT_SECONDS:
                    raise ExaRateLimitFailure(
                        f"rate limit on search {query!r}; Exa asked for a {wait_seconds}s wait, "
                        f"over this scan's {MAX_RATE_WAIT_SECONDS}s wait limit. Stopping this scan; "
                        "the search will be kept for the next scan."
                    ) from error
                if attempt == MAX_ATTEMPTS - 1:
                    raise ExaRateLimitFailure(
                        f"rate limit on search {query!r} continued after {MAX_ATTEMPTS} requests. "
                        "Stopping this scan; the search will be kept for the next scan."
                    ) from error
                wait_seconds = max(1, wait_seconds)
                logger.warning(
                    "[Exa] Search %r was rate limited (429): %s. Waiting %ss, then retrying this search.",
                    query,
                    error.detail,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
            except (_ExaServerFailure, httpx.RequestError, TimeoutError) as error:
                if attempt == MAX_ATTEMPTS - 1:
                    raise ExaSearchFailure(
                        f"search {query!r} failed after {MAX_ATTEMPTS} requests: {error}"
                    ) from error
                wait_seconds = 2 ** (attempt + 1)
                logger.warning(
                    "[Exa] Search %r failed: %s. Waiting %ss, then retrying this search.",
                    query,
                    error,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
        raise ExaSearchFailure(f"search {query!r} ended without a reply")

    async def _search(
        self, query: str, include_domains: list[str] | None = None
    ) -> list[dict[str, Any]]:
        keys = self._configured_keys()
        if not keys:
            raise ExaKeyFailure(f"no non-empty Exa API key is configured for search {query!r}")

        first_key = self._get_api_key()
        ordered_keys = [first_key] + [key for key in keys if key != first_key]
        rejected = []
        for index, key in enumerate(ordered_keys, start=1):
            try:
                return await self._request_with_retries(query, key, include_domains)
            except _ExaKeyRejected as error:
                rejected.append(error.status_code)
                if index < len(ordered_keys):
                    logger.warning(
                        "[Exa] Search %r got HTTP %s from key %s/%s. Trying the next configured key.",
                        query,
                        error.status_code,
                        index,
                        len(ordered_keys),
                    )
                    continue
                raise ExaKeyFailure(
                    f"all {len(ordered_keys)} configured Exa keys were rejected for search {query!r} "
                    f"(HTTP statuses {rejected}). Keep this search and check the Exa keys."
                ) from error
        raise ExaKeyFailure(f"no configured Exa key completed search {query!r}")

    async def search_raw(
        self,
        query: str,
        *,
        include_domains: list[str] | None = None,
        raise_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """Return raw provider results (urls/text) without token filtering."""
        try:
            return await self._search(query, include_domains)
        except Exception as error:  # noqa: BLE001 - callers decide how loud to be
            if raise_on_error:
                raise
            logger.warning(f"[Exa] Raw search {query!r} failed: {error}")
            return []

    async def search(
        self,
        query: str = '"api.telegram.org/bot"',
        *,
        raise_on_error: bool = False,
        include_domains: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            exa_results = await self._search(query, include_domains)
            results = []
            for item in exa_results:
                if not isinstance(item, dict):
                    continue
                text = item.get("text") or ""
                url = item.get("url", "")
                if not text:
                    continue
                for token in TOKEN_PATTERN.findall(text):
                    if not _is_valid_token(token):
                        continue
                    results.append({
                        "token": token,
                        "chat_id": None,
                        "meta": {"source": "exa", "url": url},
                    })
            logger.info(
                "[Exa] Search %r completed: %s provider results, %s token matches.",
                query,
                len(exa_results),
                len(results),
            )
            return results
        except ExaSearchFailure as error:
            log = logger.warning if isinstance(error, (ExaKeyFailure, ExaQuotaFailure)) else logger.error
            if raise_on_error:
                log(
                    "[Exa] Search %r failed: %s. Passing it to the Exa worker to keep for the next scan.",
                    query,
                    error,
                )
                raise
            log(
                "[Exa] Search %r failed: %s. Returning an empty list to this caller.",
                query,
                error,
            )
            return []
        except Exception as error:
            failure = ExaSearchFailure(f"search {query!r} failed: {error}")
            if raise_on_error:
                logger.error(
                    "[Exa] Search %r failed: %s. Passing it to the Exa worker to keep for the next scan.",
                    query,
                    failure,
                )
                raise failure from error
            logger.error(
                "[Exa] Search %r failed: %s. Returning an empty list to this caller.",
                query,
                failure,
            )
            return []
