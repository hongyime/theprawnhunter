"""Scanner client for GrepApp."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import math

from ..common import *

class GrepAppService:
    BOT_TOKEN_RE = re.compile(r"(\d{8,15}:[A-Za-z0-9_-]{35})")
    MAX_PAGES = 5
    MAX_RESULTS = 100
    MAX_RETRIES = 3
    MAX_BACKOFF_SECONDS = 30
    MAX_RETRY_AFTER_SECONDS = 60
    REQUEST_GAP_SECONDS = 1

    def __init__(self):
        self.base_url = "https://grep.app/api/search"

    def _strip_html(self, html: str) -> str:
        return htmlmod.unescape(re.sub(r"<[^>]+>", "", html))

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            delay = float(value)
            return delay if math.isfinite(delay) and delay >= 0 else None
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    async def _get_page(self, client, query: str, page: int, params: dict[str, str]):
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = await client.get(self.base_url, params=params)
            except httpx.RequestError:
                if attempt >= self.MAX_RETRIES:
                    raise
                delay = min(2 ** attempt, self.MAX_BACKOFF_SECONDS)
                logger.warning(
                    "[grep.app] Search %r page %d had a network error; waiting %ds before retry %d/%d.",
                    query, page, delay, attempt + 1, self.MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code == 429:
                retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after > self.MAX_RETRY_AFTER_SECONDS:
                    raise RuntimeError(
                        f"HTTP 429 asks for a {retry_after:.0f}s wait, above the "
                        f"{self.MAX_RETRY_AFTER_SECONDS}s limit for this run"
                    )
                delay = max(
                    self.REQUEST_GAP_SECONDS,
                    retry_after if retry_after is not None else min(2 ** (attempt + 1), self.MAX_BACKOFF_SECONDS),
                )
                if attempt >= self.MAX_RETRIES:
                    logger.warning(
                        "[grep.app] Search %r page %d is still rate limited after %d retries; "
                        "waiting %ds, then ending this run. The next scan will try again.",
                        query, page, self.MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    raise RuntimeError(f"HTTP 429 remained after {self.MAX_RETRIES} retries")
                logger.warning(
                    "[grep.app] Search %r page %d was rate limited; waiting %ds before retry %d/%d.",
                    query, page, delay, attempt + 1, self.MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 500 and attempt < self.MAX_RETRIES:
                delay = min(2 ** attempt, self.MAX_BACKOFF_SECONDS)
                logger.warning(
                    "[grep.app] Search %r page %d returned HTTP %d; waiting %ds before retry %d/%d.",
                    query, page, response.status_code, delay, attempt + 1, self.MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            response.raise_for_status()
            return response

        raise RuntimeError("request retry loop ended unexpectedly")

    async def search(self) -> list[dict[str, Any]]:
        query = r"api\.telegram\.org/bot\d{8,15}:[A-Za-z0-9_-]{35}"
        page = 1

        try:
            from app.services.scanners import _is_valid_token
            results = []
            seen = set()
            processed_hits = 0

            async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
                for page in range(1, self.MAX_PAGES + 1):
                    params = {"q": query, "regexp": "true", "page": page}
                    res = await self._get_page(client, query, page, params)
                    data = res.json()
                    if not isinstance(data, dict):
                        raise ValueError("reply was not a JSON object")
                    if data.get("error"):
                        raise ValueError(f"reply contained an error: {data['error']}")

                    hit_group = data.get("hits")
                    if not isinstance(hit_group, dict):
                        raise ValueError("reply did not contain a hits object")
                    hits = hit_group.get("hits")
                    if not isinstance(hits, list):
                        raise ValueError("reply did not contain a hits list")
                    if not hits:
                        logger.info("[grep.app] Search %r returned no hits on page %d.", query, page)
                        break

                    for hit in hits:
                        if processed_hits >= self.MAX_RESULTS or len(results) >= self.MAX_RESULTS:
                            break
                        processed_hits += 1
                        snippet = hit.get("content", {}).get("snippet", "")
                        clean = self._strip_html(snippet)
                        for t in self.BOT_TOKEN_RE.findall(clean):
                            if t not in seen and _is_valid_token(t):
                                seen.add(t)
                                results.append({
                                    "token": t,
                                    "meta": {"source": "grep.app", "repo": hit.get("repo")}
                                })
                                if len(results) >= self.MAX_RESULTS:
                                    break

                    if processed_hits >= self.MAX_RESULTS or len(results) >= self.MAX_RESULTS:
                        logger.info(
                            "[grep.app] Search %r reached the %d-result cap; ending pagination.",
                            query, self.MAX_RESULTS,
                        )
                        break

                    if page < self.MAX_PAGES:
                        await asyncio.sleep(self.REQUEST_GAP_SECONDS)

            logger.info("[grep.app] Search %r finished with %d matches.", query, len(results))
            return results
        except Exception as e:
            message = (
                f"GrepApp search page {page} (Telegram bot tokens) failed: "
                f"{type(e).__name__}: {e}. It will be tried again on the next scan."
            )
            logger.error("[grep.app] %s | query=%r", message, query)
            raise RuntimeError(message) from e
