"""Scanner client for Rentry."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from ..common import *


RETRY_PASTE_SET = "rentry:retry:paste_ids"
SEEN_TTL_SECONDS = 7 * 86400
MAX_FETCH_ATTEMPTS = 4
MAX_RATE_WAIT_SECONDS = 60
REQUEST_GAP_SECONDS = 1


class RentryService:
    """Find Telegram tokens in Rentry pastes found through Exa."""

    def __init__(self):
        from app.services.scanners import ExaService

        self.exa = ExaService()
        self.redis_client = None

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

    @staticmethod
    def _body_error(text: str) -> str | None:
        body = (text or "").strip()
        if not body:
            return "empty reply; the paste may be missing or still loading"

        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).casefold()
            if status == "loading":
                return "paste is still loading"
            if payload.get("error") or payload.get("success") is False or status in {"error", "not_found"}:
                return "HTTP 200 reply contains an error status"

        # Error pages are short. Keep this check on short replies so normal paste
        # text that discusses errors or loading does not get held for a retry.
        if len(body) <= 500:
            first_line = " ".join(body.splitlines()).casefold()
            loading_starts = ("still loading", "loading", "please wait", "processing")
            if first_line.startswith(loading_starts):
                return "paste is still loading"
            error_starts = (
                "error:",
                "not found",
                "page not found",
                "paste not found",
                "this entry does not exist",
                "this paste does not exist",
                "entry not found",
                "too many requests",
                "rate limit",
                "temporarily unavailable",
            )
            if first_line.startswith(error_starts):
                return "HTTP 200 reply looks like an error page"
        return None

    @staticmethod
    def _paste_id(url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except (TypeError, ValueError):
            return None
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in {
            "rentry.co",
            "www.rentry.co",
        }:
            return None

        parts = [part for part in parsed.path.split("/") if part]
        if parts[:2] == ["api", "raw"]:
            parts = parts[2:]
        if len(parts) != 1:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
            return None
        return parts[0]

    def _keep_for_next_scan(self, paste_id: str, reason: str) -> None:
        try:
            self.redis_client.sadd(RETRY_PASTE_SET, paste_id)
        except Exception as error:
            logger.error(
                "[Rentry] Could not keep paste %r for the next scan after %s: %s. "
                "A later Exa search may find it again.",
                paste_id,
                reason,
                error,
            )

    def _mark_complete(self, paste_id: str) -> bool:
        seen_key = f"rentry:seen:{paste_id}"
        try:
            self.redis_client.setex(seen_key, SEEN_TTL_SECONDS, "1")
        except Exception as error:
            self._keep_for_next_scan(paste_id, "could not save the completed mark")
            logger.error(
                "[Rentry] Could not record paste %r as complete: %s. Keeping it for the next scan.",
                paste_id,
                error,
            )
            return False

        try:
            self.redis_client.srem(RETRY_PASTE_SET, paste_id)
        except Exception as error:
            logger.warning(
                "[Rentry] Paste %r was read, but its retry mark could not be cleared: %s. "
                "The seen mark will prevent another fetch.",
                paste_id,
                error,
            )
        return True

    def keep_unsaved_results(self, results: list[dict[str, Any]], error: Exception) -> None:
        paste_ids = {
            item.get("meta", {}).get("paste_id")
            for item in results
            if isinstance(item, dict) and isinstance(item.get("meta"), dict)
        }
        for paste_id in paste_ids - {None, ""}:
            self._keep_for_next_scan(paste_id, "result saving failed")
            logger.error(
                "[Rentry] Could not save results from paste %r: %s. Keeping the paste for the next scan.",
                paste_id,
                error,
            )

    def mark_saved_results(self, results: list[dict[str, Any]]) -> None:
        paste_ids = {
            item.get("meta", {}).get("paste_id")
            for item in results
            if isinstance(item, dict) and isinstance(item.get("meta"), dict)
        }
        for paste_id in paste_ids - {None, ""}:
            self._mark_complete(paste_id)

    async def _fetch_paste(self, client, paste_id: str, url: str) -> tuple[list[dict[str, Any]], bool]:
        raw_url = f"https://rentry.co/api/raw/{paste_id}"
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            try:
                response = await client.get(raw_url)
            except Exception as error:
                if attempt == MAX_FETCH_ATTEMPTS:
                    self._keep_for_next_scan(paste_id, "raw fetch failed")
                    logger.error(
                        "[Rentry] Raw fetch for paste %r (%s) failed after %s requests: %s. "
                        "Keeping it for the next scan.",
                        paste_id,
                        url,
                        MAX_FETCH_ATTEMPTS,
                        error,
                    )
                    return [], False
                wait_seconds = 2 ** attempt
                logger.warning(
                    "[Rentry] Raw fetch for paste %r failed: %s. Waiting %ss, then trying it again.",
                    paste_id,
                    error,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue

            status = response.status_code
            if status == 429:
                retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                wait_seconds = retry_after if retry_after is not None else 2 ** attempt
                if wait_seconds > MAX_RATE_WAIT_SECONDS:
                    self._keep_for_next_scan(paste_id, "Rentry asked for a long rate-limit wait")
                    logger.warning(
                        "[Rentry] Paste %r was rate limited and Rentry asked for %ss. Waiting %ss, "
                        "then stopping raw fetches; saved pastes will be tried next scan.",
                        paste_id,
                        wait_seconds,
                        MAX_RATE_WAIT_SECONDS,
                    )
                    await asyncio.sleep(MAX_RATE_WAIT_SECONDS)
                    return [], True
                if attempt == MAX_FETCH_ATTEMPTS:
                    self._keep_for_next_scan(paste_id, "rate limit continued")
                    logger.warning(
                        "[Rentry] Paste %r was still rate limited after %s requests. "
                        "Stopping raw fetches now; saved pastes will be tried next scan.",
                        paste_id,
                        MAX_FETCH_ATTEMPTS,
                    )
                    return [], True
                wait_seconds = max(1, wait_seconds)
                logger.warning(
                    "[Rentry] Paste %r was rate limited (HTTP 429). Waiting %ss, then trying it again.",
                    paste_id,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue

            if status in {408, 425} or status >= 500:
                if attempt < MAX_FETCH_ATTEMPTS:
                    wait_seconds = 2 ** attempt
                    logger.warning(
                        "[Rentry] Paste %r returned HTTP %s. Waiting %ss, then trying it again.",
                        paste_id,
                        status,
                        wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                self._keep_for_next_scan(paste_id, f"HTTP {status}")
                logger.error(
                    "[Rentry] Paste %r returned HTTP %s after %s requests. Keeping it for the next scan.",
                    paste_id,
                    status,
                    MAX_FETCH_ATTEMPTS,
                )
                return [], False

            if status != 200:
                self._keep_for_next_scan(paste_id, f"HTTP {status}")
                if status in {404, 410}:
                    detail = "paste is missing or deleted"
                elif status in {202, 204}:
                    detail = "paste is still loading or has no reply body"
                elif status in {401, 403}:
                    detail = "Rentry denied access"
                else:
                    detail = "raw fetch did not return a successful reply"
                logger.warning(
                    "[Rentry] Paste %r returned HTTP %s (%s). Keeping it for the next scan.",
                    paste_id,
                    status,
                    detail,
                )
                return [], False

            body = response.text
            body_error = self._body_error(body)
            if body_error:
                self._keep_for_next_scan(paste_id, body_error)
                logger.warning(
                    "[Rentry] Paste %r returned HTTP 200, but %s. Keeping it for the next scan.",
                    paste_id,
                    body_error,
                )
                return [], False

            found = []
            for token in TOKEN_PATTERN.findall(body):
                if _is_valid_token(token):
                    found.append({
                        "token": token,
                        "meta": {"source": "rentry", "paste_id": paste_id, "url": url},
                    })

            if found:
                logger.info(
                    "[Rentry] Read paste %r and found %s token matches. It stays saved until results are saved.",
                    paste_id,
                    len(found),
                )
            else:
                logger.info(
                    "[Rentry] Read paste %r; it has no token matches. Marking it complete for %s days.",
                    paste_id,
                    SEEN_TTL_SECONDS // 86400,
                )
                self._mark_complete(paste_id)
            return found, False

        return [], False

    async def search(self) -> list[dict[str, Any]]:
        from app.workers.tasks.flow_tasks import redis_client

        self.redis_client = redis_client
        results: list[dict[str, Any]] = []
        queries = [
            'site:rentry.co "api.telegram.org/bot"',
            'site:rentry.co "TELEGRAM_BOT_TOKEN"',
        ]
        paste_urls: dict[str, str] = {}

        try:
            pending = redis_client.smembers(RETRY_PASTE_SET)
        except Exception as error:
            pending = set()
            logger.error(
                "[Rentry] Could not load saved paste work for this scan: %s. "
                "Fresh Exa results will still be checked.",
                error,
            )
        for value in pending:
            try:
                paste_id = value.decode() if isinstance(value, bytes) else str(value)
            except UnicodeDecodeError as error:
                logger.warning(
                    "[Rentry] Could not read a saved paste ID: %s. Other saved and fresh pastes will continue.",
                    error,
                )
                continue
            if paste_id:
                if re.fullmatch(r"[A-Za-z0-9_-]+", paste_id):
                    paste_urls[paste_id] = f"https://rentry.co/{paste_id}"
                else:
                    logger.warning(
                        "[Rentry] Saved paste ID %r is not valid. Leaving it saved and checking other pastes.",
                        paste_id,
                    )

        for query_index, query in enumerate(queries):
            try:
                exa_results = await self.exa.search(query, raise_on_error=True)
            except Exception as error:
                logger.error(
                    "[Rentry] Search %r failed: %s. This search will run again next scan; "
                    "checking the other Rentry search now.",
                    query,
                    error,
                )
                exa_results = None

            if exa_results is not None:
                if not exa_results:
                    logger.info("[Rentry] Search %r returned no matches.", query)
                for match in exa_results:
                    if not isinstance(match, dict) or not isinstance(match.get("meta"), dict):
                        logger.warning(
                            "[Rentry] Search %r returned a result without paste details. Skipping that result.",
                            query,
                        )
                        continue
                    url = match["meta"].get("url")
                    paste_id = self._paste_id(url) if isinstance(url, str) else None
                    if not paste_id:
                        logger.warning(
                            "[Rentry] Search %r returned a result without a valid Rentry paste URL: %r. "
                            "Skipping that result.",
                            query,
                            url,
                        )
                        continue
                    paste_urls.setdefault(paste_id, url)

            if query_index < len(queries) - 1:
                await asyncio.sleep(REQUEST_GAP_SECONDS)

        if not paste_urls:
            return results

        # Save every discovered paste before fetching so a worker restart does
        # not lose work if Exa stops returning the URL on a later scan.
        for paste_id in paste_urls:
            self._keep_for_next_scan(paste_id, "paste was found and is waiting to be read")

        try:
            paste_ids = list(paste_urls)
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                for index, paste_id in enumerate(paste_ids):
                    url = paste_urls[paste_id]
                    seen_key = f"rentry:seen:{paste_id}"
                    try:
                        already_seen = redis_client.exists(seen_key)
                    except Exception as error:
                        already_seen = False
                        logger.warning(
                            "[Rentry] Could not check whether paste %r was already read: %s. "
                            "Fetching it now and keeping it saved until complete.",
                            paste_id,
                            error,
                        )
                    if already_seen:
                        try:
                            redis_client.srem(RETRY_PASTE_SET, paste_id)
                        except Exception as error:
                            logger.warning(
                                "[Rentry] Paste %r was already read, but its saved retry mark could not be cleared: %s.",
                                paste_id,
                                error,
                            )
                        continue

                    found, stop_raw_fetches = await self._fetch_paste(client, paste_id, url)
                    results.extend(found)
                    if stop_raw_fetches:
                        remaining = len(paste_ids) - index - 1
                        logger.warning(
                            "[Rentry] Leaving %s later paste(s) saved for the next scan after the rate limit.",
                            remaining,
                        )
                        break
                    if index < len(paste_ids) - 1:
                        await asyncio.sleep(REQUEST_GAP_SECONDS)
        except Exception as error:
            logger.error(
                "[Rentry] Could not continue raw paste fetching for %r: %s. "
                "All saved paste work stays for the next scan.",
                paste_ids,
                error,
            )

        return results
