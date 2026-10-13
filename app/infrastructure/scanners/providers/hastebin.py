"""Scanner client for Hastebin."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import html
import json
import re
from urllib.parse import urlparse

from ..common import *


class HastebinService:
    """Search Hastebin through Exa and fetch each matching raw paste."""

    PENDING_KEY = "hastebin:pending"
    SEEN_TTL_SECONDS = 7 * 86400
    MAX_FETCH_ATTEMPTS = 3
    MAX_RATE_WAIT_SECONDS = 60

    def __init__(self):
        from app.services.scanners import ExaService

        self.exa = ExaService()

    @staticmethod
    def _paste_id(url: str) -> str | None:
        try:
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower().rstrip(".")
        except (TypeError, ValueError):
            return None
        if parsed.scheme not in {"http", "https"}:
            return None
        if hostname.startswith("www."):
            hostname = hostname[4:]
        if hostname != "hastebin.com":
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].lower() == "raw":
            parts = parts[1:]
        if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
            return None
        return parts[0]

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
    def _reply_problem(response) -> str | None:
        text = response.text or ""
        if not text.strip():
            return "empty HTTP 200 reply"

        content_type = response.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            try:
                body = json.loads(text)
            except (TypeError, ValueError):
                body = None
            if isinstance(body, dict) and (body.get("error") or body.get("ok") is False):
                detail = str(body)[:200]
                if any(
                    marker in detail.casefold()
                    for marker in ("too many requests", "rate limit", "rate limited", "asking too fast")
                ):
                    return f"rate-limit error in HTTP 200 reply: {detail}"
                return f"error in HTTP 200 reply: {str(body)[:200]}"

        plain_text = html.unescape(re.sub(r"<[^>]*>", " ", text)).strip().lower()
        first_line = " ".join(plain_text.splitlines())[:500]
        if first_line.startswith(
            ("too many requests", "rate limit", "rate limited", "you are asking too fast")
        ):
            return f"rate-limit error in HTTP 200 reply: {text.strip()[:200]}"
        if re.match(
            r"^(?:loading\b|please wait\b|404\b|error\s*404\b|not found\b|"
            r"(?:document|paste|haste)\s+(?:was\s+)?(?:not found|does not exist|doesn't exist|has been deleted)\b)",
            plain_text,
        ):
            return f"possible missing, deleted, or loading reply: {text.strip()[:200]}"
        if re.search(r"<title[^>]*>\s*(?:404|not found|loading)", text, re.IGNORECASE):
            return f"possible missing, deleted, or loading reply: {text.strip()[:200]}"
        return None

    def _load_pending(self) -> set[str]:
        from app.workers.tasks.flow_tasks import redis_client

        try:
            values = redis_client.smembers(self.PENDING_KEY) or set()
            return {
                value.decode() if isinstance(value, bytes) else str(value)
                for value in values
            }
        except Exception as error:
            logger.error(
                "[Hastebin] Could not read the pending paste list: %s. Will use this scan's Exa results; saved pastes can be retried when Redis is available.",
                error,
            )
            return set()

    def _keep_pending(self, paste_id: str) -> bool:
        from app.workers.tasks.flow_tasks import redis_client

        try:
            redis_client.sadd(self.PENDING_KEY, paste_id)
            return True
        except Exception as error:
            logger.error(
                "[Hastebin] Could not save paste %s for the next scan: %s. Skipping its fetch; Exa will be searched again next scan.",
                paste_id,
                error,
            )
            return False

    def _remove_pending(self, paste_id: str) -> None:
        from app.workers.tasks.flow_tasks import redis_client

        try:
            redis_client.srem(self.PENDING_KEY, paste_id)
        except Exception as error:
            logger.error(
                "[Hastebin] Could not remove completed paste %s from the pending list: %s. Its seen marker will prevent another fetch.",
                paste_id,
                error,
            )

    def _mark_complete(self, paste_id: str) -> bool:
        from app.workers.tasks.flow_tasks import redis_client

        try:
            redis_client.setex(
                f"hastebin:seen:{paste_id}", self.SEEN_TTL_SECONDS, "1"
            )
        except Exception as error:
            logger.error(
                "[Hastebin] Could not mark paste %s as read: %s. It remains pending for the next scan.",
                paste_id,
                error,
            )
            return False
        self._remove_pending(paste_id)
        return True

    def mark_saved(self, results: list[dict[str, Any]]) -> None:
        """Mark token-bearing pastes complete only after their results are saved."""
        paste_ids = {
            item.get("meta", {}).get("paste_id")
            for item in results
            if isinstance(item, dict) and isinstance(item.get("meta"), dict)
        }
        for paste_id in sorted(value for value in paste_ids if value):
            self._mark_complete(paste_id)

    async def _fetch_raw(self, client, paste_id: str):
        raw_url = f"https://hastebin.com/raw/{paste_id}"
        for attempt in range(1, self.MAX_FETCH_ATTEMPTS + 1):
            try:
                response = await client.get(raw_url)
            except Exception as error:
                if attempt < self.MAX_FETCH_ATTEMPTS:
                    wait_seconds = 2**attempt
                    logger.warning(
                        "[Hastebin] Paste %s fetch failed: %s. Waiting %ss before retry %s/%s.",
                        paste_id,
                        error,
                        wait_seconds,
                        attempt + 1,
                        self.MAX_FETCH_ATTEMPTS,
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                return None, f"connection failed after {self.MAX_FETCH_ATTEMPTS} requests: {error}", False

            if response.status_code == 200:
                return response, None, False

            if response.status_code == 429:
                retry_after = self._retry_after_seconds(
                    response.headers.get("Retry-After")
                )
                wait_seconds = retry_after if retry_after is not None else 2**attempt
                if wait_seconds > self.MAX_RATE_WAIT_SECONDS:
                    return (
                        None,
                        f"HTTP 429 asks for a {wait_seconds}s wait, above the {self.MAX_RATE_WAIT_SECONDS}s limit",
                        True,
                    )
                if attempt == self.MAX_FETCH_ATTEMPTS:
                    return (
                        None,
                        f"HTTP 429 continued after {self.MAX_FETCH_ATTEMPTS} requests",
                        True,
                    )
                wait_seconds = max(1, wait_seconds)
                logger.warning(
                    "[Hastebin] Paste %s was rate limited (HTTP 429). Waiting %ss, then retrying it.",
                    paste_id,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue

            if response.status_code >= 500 and attempt < self.MAX_FETCH_ATTEMPTS:
                wait_seconds = 2**attempt
                logger.warning(
                    "[Hastebin] Paste %s returned HTTP %s. Waiting %ss before retry %s/%s.",
                    paste_id,
                    response.status_code,
                    wait_seconds,
                    attempt + 1,
                    self.MAX_FETCH_ATTEMPTS,
                )
                await asyncio.sleep(wait_seconds)
                continue

            detail = f"HTTP {response.status_code}"
            if response.status_code in (404, 410):
                detail += "; paste is missing or deleted"
            return None, detail, False

        return None, f"fetch ended after {self.MAX_FETCH_ATTEMPTS} requests", False

    async def _scan_paste(self, client, paste_id: str, url: str) -> tuple[list[dict[str, Any]], bool]:
        from app.services.scanners import TOKEN_PATTERN, _is_valid_token
        from app.workers.tasks.flow_tasks import redis_client

        try:
            seen_key = f"hastebin:seen:{paste_id}"
            try:
                if redis_client.exists(seen_key):
                    self._remove_pending(paste_id)
                    return [], False
            except Exception as error:
                logger.warning(
                    "[Hastebin] Could not check whether paste %s was already read: %s. Will try the fetch and keep it pending until completion.",
                    paste_id,
                    error,
                )

            response, problem, stop_for_rate_limit = await self._fetch_raw(
                client, paste_id
            )
            if problem:
                logger.error(
                    "[Hastebin] Paste %s fetch failed: %s. It remains pending for the next scan.%s",
                    paste_id,
                    problem,
                    " Stopping raw fetches for this run to respect the rate limit." if stop_for_rate_limit else "",
                )
                if not stop_for_rate_limit:
                    await asyncio.sleep(1)
                return [], stop_for_rate_limit

            body_problem = self._reply_problem(response)
            if body_problem:
                rate_limited = body_problem.startswith("rate-limit")
                logger.warning(
                    "[Hastebin] Paste %s returned %s. It remains pending for the next scan.%s",
                    paste_id,
                    body_problem,
                    " Stopping raw fetches for this run to respect the rate limit." if rate_limited else "",
                )
                if not rate_limited:
                    await asyncio.sleep(1)
                return [], rate_limited

            found_tokens = TOKEN_PATTERN.findall(response.text)
            valid_tokens = [token for token in found_tokens if _is_valid_token(token)]
            results = [
                {
                    "token": token,
                    "meta": {"source": "hastebin", "paste_id": paste_id, "url": url},
                }
                for token in valid_tokens
            ]
            if not results:
                self._mark_complete(paste_id)

            await asyncio.sleep(1)
            return results, False
        except Exception as error:
            logger.error(
                "[Hastebin] Paste %s could not be processed: %s. It remains pending for the next scan; continuing with other pastes.",
                paste_id,
                error,
            )
            await asyncio.sleep(1)
            return [], False

    async def search(self) -> list[dict[str, Any]]:
        from app.services.scanners import ExaKeyFailure, ExaQuotaFailure, ExaRateLimitFailure

        results: list[dict[str, Any]] = []
        queries = [
            'site:hastebin.com "api.telegram.org/bot"',
            'site:hastebin.com "TELEGRAM_BOT_TOKEN"',
        ]
        pending_ids = self._load_pending()
        urls_by_id = {
            paste_id: f"https://hastebin.com/{paste_id}" for paste_id in pending_ids
        }

        for query in queries:
            try:
                exa_results = await self.exa.search(query, raise_on_error=True)
            except (ExaRateLimitFailure, ExaQuotaFailure, ExaKeyFailure) as error:
                logger.warning(
                    "[Hastebin] Search %r stopped: %s. Remaining searches will run next scan.",
                    query,
                    error,
                )
                break
            except Exception as error:
                logger.error(
                    "[Hastebin] Search %r failed: %s. It will run again next scan; continuing with the other search.",
                    query,
                    error,
                )
                continue

            if not exa_results:
                logger.info("[Hastebin] Search %r returned no token matches.", query)
                continue

            for match in exa_results:
                if not isinstance(match, dict) or not isinstance(match.get("meta"), dict):
                    logger.warning(
                        "[Hastebin] Search %r returned a result without paste details. Skipping it; the search will run again next scan.",
                        query,
                    )
                    continue
                url = match["meta"].get("url")
                if not isinstance(url, str) or not url:
                    logger.warning(
                        "[Hastebin] Search %r returned a result without a URL. Skipping it; the search will run again next scan.",
                        query,
                    )
                    continue
                paste_id = self._paste_id(url)
                if not paste_id:
                    logger.warning(
                        "[Hastebin] Search %r returned an invalid Hastebin URL %r. Skipping it; the search will run again next scan.",
                        query,
                        url,
                    )
                    continue
                if self._keep_pending(paste_id):
                    pending_ids.add(paste_id)
                    urls_by_id[paste_id] = url

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                for paste_id in sorted(pending_ids):
                    paste_results, stop_for_rate_limit = await self._scan_paste(
                        client,
                        paste_id,
                        urls_by_id.get(
                            paste_id, f"https://hastebin.com/{paste_id}"
                        ),
                    )
                    results.extend(paste_results)
                    if stop_for_rate_limit:
                        break
        except Exception as error:
            logger.error(
                "[Hastebin] Raw paste service could not be reached: %s. All unfinished pastes remain pending for the next scan.",
                error,
            )

        return results
