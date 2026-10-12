"""Scanner client for Searchcode."""

from ..common import *

class SearchcodeService:
    """Searchcode public REST API adapter — queries codesearch_I for Telegram bot token leaks."""

    DEFAULT_QUERIES = ["api.telegram.org/bot", "TELEGRAM_BOT_TOKEN="]

    def __init__(self):
        self.timeout = httpx.Timeout(30.0, connect=10.0)

    async def search(self, query: str = None) -> list[dict[str, Any]]:
        from app.services.scanners import (
            TOKEN_PATTERN,
            _is_valid_token,
            extract_infrastructure_context,
        )

        queries = [query] if query else self.DEFAULT_QUERIES
        results: list[dict[str, Any]] = []
        seen_tokens: set = set()

        async with get_async_http_client(timeout=self.timeout, follow_redirects=True) as client:
            for q in queries:
                try:
                    res = await client.get(
                        "https://searchcode.com/api/codesearch_I/",
                        params={"q": q, "p": 0, "per_page": 100},
                    )
                    if res.status_code != 200:
                        logger.debug(f"[Searchcode] q='{q[:30]}' HTTP {res.status_code}")
                        continue

                    for item in res.json().get("results", []):
                        lines = item.get("lines", {})
                        text_block = "\n".join(str(v) for v in lines.values())
                        source_meta = {
                            "source": "searchcode",
                            "domain": "searchcode.com",
                            "query": q,
                            "repository": item.get("repo") or item.get("repository"),
                            "path": item.get("filename") or item.get("name") or item.get("path"),
                        }
                        for line_text in lines.values():
                            for match in TOKEN_PATTERN.findall(str(line_text)):
                                if match not in seen_tokens and _is_valid_token(match):
                                    seen_tokens.add(match)
                                    meta = dict(source_meta)
                                    infrastructure_context = extract_infrastructure_context(
                                        text_block,
                                        source_meta,
                                        token=match,
                                    )
                                    if infrastructure_context:
                                        meta["infrastructure_context"] = infrastructure_context
                                    results.append({
                                        "token": match,
                                        "meta": meta,
                                    })
                except Exception as e:
                    logger.warning(f"[Searchcode] Error for query '{q[:30]}': {e}")

        logger.info(f"[Searchcode] returned {len(results)} matches")
        return results
