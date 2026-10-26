"""Scanner client for a self-hosted SearXNG meta-search instance."""

from ..common import *


class SearxngService:
    """Query a local SearXNG instance and sweep result snippets for tokens.

    SearXNG aggregates several engines (Google/Bing/Brave/DDG) behind one
    key-free JSON endpoint, which replaces query-based APIs that are closed
    to new projects.
    """

    QUERIES = [
        '"api.telegram.org/bot"',
        '"TELEGRAM_BOT_TOKEN"',
        'telegram bot token pastebin',
        'telegram bot token site:github.com',
    ]
    MAX_RESULTS_PER_QUERY = 20
    MAX_QUERIES_PER_RUN = 4

    def __init__(self):
        base = getattr(settings, "SEARXNG_URL", "http://searxng:8080") or "http://searxng:8080"
        self.base_url = base.rstrip("/")
        self.timeout = httpx.Timeout(25.0, connect=8.0)

    async def search(self, query: str | None = None) -> list[dict[str, Any]]:
        from app.domain.tokens import sweep_text_for_tokens

        results: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        queries = [query] if query else self.QUERIES[: self.MAX_QUERIES_PER_RUN]

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for search_query in queries:
                try:
                    response = await client.get(
                        f"{self.base_url}/search",
                        params={"q": search_query, "format": "json", "safesearch": 0},
                    )
                except Exception as error:  # noqa: BLE001 - one query must not stop the run
                    logger.warning(f"[SearXNG] Search {search_query!r} failed: {error}")
                    continue

                if response.status_code != 200:
                    logger.warning(
                        f"[SearXNG] Search {search_query!r} returned HTTP {response.status_code}; skipping."
                    )
                    continue

                try:
                    payload = response.json()
                except Exception:  # noqa: BLE001
                    logger.warning(f"[SearXNG] Search {search_query!r} returned non-JSON; skipping.")
                    continue

                for item in (payload.get("results") or [])[: self.MAX_RESULTS_PER_QUERY]:
                    if not isinstance(item, dict):
                        continue
                    text = "\n".join(
                        part for part in (item.get("title"), item.get("content")) if part
                    )
                    if not text:
                        continue
                    for hit in sweep_text_for_tokens(text, "searxng"):
                        token = hit["token"]
                        if token in seen_tokens:
                            continue
                        seen_tokens.add(token)
                        meta: dict[str, Any] = {
                            "source": "searxng",
                            "url": item.get("url"),
                            "engine": item.get("engine"),
                            "query": search_query,
                        }
                        if hit.get("chat_id"):
                            meta["chat_id"] = hit["chat_id"]
                        results.append({"token": token, "meta": meta})

                await asyncio.sleep(1)

        if results:
            logger.info(f"[SearXNG] returned {len(results)} candidate(s)")
        return results
