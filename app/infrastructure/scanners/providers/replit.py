"""Scanner client for Replit."""

from ..common import *

class ReplitService:
    """
    Replit public search — free, no API key required.
    Searches public repls matching Telegram-related queries, then fetches
    common entry-point files (main.py, bot.py, etc.) and extracts tokens.

    Rate: undocumented; 2s/repl as courtesy.
    """

    SEARCH_URL = "https://replit.com/graphql"
    REPL_RAW_BASE = "https://replit.com"

    QUERIES = [
        "telegram bot token",
        "TELEGRAM_BOT_TOKEN",
        "api.telegram.org",
    ]

    ENTRY_FILES = ("main.py", "index.js", "bot.py", "app.py", ".env", "config.py", "settings.py")

    def __init__(self):
        self.timeout = httpx.Timeout(20.0, connect=10.0)

    async def search(self, query: str = None) -> list[dict[str, Any]]:
        import hashlib

        from app.services.scanners import (
            TOKEN_PATTERN,
            _is_valid_token,
        )
        from app.workers.tasks.flow_tasks import redis_client

        results: list[dict[str, Any]] = []
        queries = [query] if query else self.QUERIES

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as client:
            for q in queries:
                try:
                    # Replit GraphQL search for public repls
                    gql_payload = {
                        "operationName": "ReplSearch",
                        "query": """
                            query ReplSearch($query: String!) {
                                search(query: $query, categories: [Repls]) {
                                    replResults {
                                        results {
                                            items {
                                                ... on Repl {
                                                    slug
                                                    user { username }
                                                    url
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        """,
                        "variables": {"query": q},
                    }
                    r = await client.post(
                        self.SEARCH_URL,
                        json=gql_payload,
                        headers={"Content-Type": "application/json"},
                    )
                    if r.status_code != 200:
                        logger.debug(f"[Replit] q='{q[:30]}' HTTP {r.status_code}")
                        await asyncio.sleep(3)
                        continue

                    data = r.json()
                    items = (
                        ((data.get("data") or {}).get("search") or {})
                        .get("replResults", {})
                        .get("results", {})
                        .get("items", [])
                    )
                except Exception as e:
                    logger.warning(f"[Replit] search failed for '{q[:30]}': {e}")
                    await asyncio.sleep(3)
                    continue

                for repl in items[:30]:
                    user = (repl.get("user") or {}).get("username") or "unknown"
                    slug = repl.get("slug")
                    if not slug:
                        continue
                    repl_url = repl.get("url") or f"/@{user}/{slug}"

                    # Dedupe by repl URL
                    h = hashlib.sha256(f"{user}/{slug}".encode()).hexdigest()[:16]
                    redis_key = f"replit:seen:{h}"
                    try:
                        if redis_client.exists(redis_key):
                            continue
                    except Exception as _swallowed:
                        logger.debug(f"[suppressed] {_swallowed}")

                    # Fetch common entry-point files
                    for filename in self.ENTRY_FILES:
                        raw_url = f"{self.REPL_RAW_BASE}/@{user}/{slug}/raw/{filename}"
                        try:
                            fr = await client.get(raw_url)
                            if fr.status_code != 200 or not fr.text:
                                continue
                            tokens = set(TOKEN_PATTERN.findall(fr.text))
                            for tok in tokens:
                                if not _is_valid_token(tok):
                                    continue
                                results.append({
                                    "token": tok,
                                    "meta": {
                                        "replit_url": f"{self.REPL_RAW_BASE}{repl_url}",
                                        "replit_file": filename,
                                        "replit_user": user,
                                        "extracted_from": "body",
                                    },
                                })
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                        await asyncio.sleep(1)

                    with contextlib.suppress(Exception):
                        redis_client.setex(redis_key, 7 * 86400, "1")

                    await asyncio.sleep(2)
                await asyncio.sleep(3)

        logger.info(f"[Replit] returned {len(results)} matches")
        return results
