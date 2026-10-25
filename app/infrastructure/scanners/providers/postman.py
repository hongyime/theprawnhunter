"""Scanner client for Postman."""

from app.domain.tokens import sweep_text_for_tokens

from ..common import *

class PostmanService:
    """
    Postman public network search via proxy API + official collection fetch.
    Requires POSTMAN_API_KEY (PMAK-...). Searches public workspaces/collections
    for Telegram bot tokens, then fetches collection bodies for extraction.
    """

    PROXY_URL = "https://www.postman.com/_api/ws/proxy"
    API_URL = "https://api.getpostman.com"

    QUERIES = [
        "telegram bot token",
        "TELEGRAM_BOT_TOKEN",
        "api.telegram.org/bot",
    ]

    DAILY_BUDGET = 250
    REDIS_COUNTER_KEY = "postman:daily_requests"

    def __init__(self):
        self.api_key = settings.POSTMAN_API_KEY
        self.timeout = httpx.Timeout(20.0, connect=10.0)

    def _today_key(self) -> str:
        from datetime import date
        return f"{self.REDIS_COUNTER_KEY}:{date.today().isoformat()}"

    def _check_budget(self, redis_client) -> bool:
        try:
            val = redis_client.get(self._today_key())
            return int(val or 0) < self.DAILY_BUDGET
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            return True

    def _increment(self, redis_client):
        try:
            key = self._today_key()
            redis_client.incr(key)
            redis_client.expire(key, 86400 * 2)
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")

    async def search(self, query: str = None) -> list[dict[str, Any]]:
        import hashlib

        from app.workers.tasks.flow_tasks import redis_client

        if not self.api_key:
            logger.warning("[Postman] No POSTMAN_API_KEY configured")
            return []

        results: list[dict[str, Any]] = []
        queries = [query] if query else self.QUERIES
        seen_tokens: set = set()

        if not self._check_budget(redis_client):
            logger.info("[Postman] Daily budget exhausted — skipping")
            return []

        headers = {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=headers,
        ) as client:
            for q in queries:
                if not self._check_budget(redis_client):
                    break

                # Step 1: Search public network via proxy (returns collections + workspaces)
                collection_ids = []
                try:
                    payload = {
                        "service": "search",
                        "method": "POST",
                        "path": "/search-all",
                        "body": {"queryText": q},
                    }
                    r = await client.post(self.PROXY_URL, json=payload)
                    self._increment(redis_client)

                    if r.status_code != 200:
                        logger.debug(f"[Postman] q='{q[:30]}' HTTP {r.status_code}")
                        await asyncio.sleep(2)
                        continue

                    data = r.json().get("data", {})
                    for col in data.get("collection", []):
                        doc = col.get("document", {})
                        cid = doc.get("id")
                        if cid:
                            collection_ids.append((cid, doc.get("name", "")))

                    for req in data.get("request", []):
                        doc = req.get("document", {})
                        cid = doc.get("collectionId")
                        if cid:
                            collection_ids.append((cid, doc.get("name", "")))

                except Exception as e:
                    logger.warning(f"[Postman] search failed for '{q[:30]}': {e}")
                    await asyncio.sleep(2)
                    continue

                # Step 2: Fetch each collection body via official API
                for collection_id, col_name in collection_ids[:15]:
                    h = hashlib.sha256(str(collection_id).encode()).hexdigest()[:16]
                    redis_key = f"postman:seen:{h}"
                    try:
                        if redis_client.exists(redis_key):
                            continue
                    except Exception as _swallowed:
                        logger.debug(f"[suppressed] {_swallowed}")

                    if not self._check_budget(redis_client):
                        break

                    try:
                        cr = await client.get(f"{self.API_URL}/collections/{collection_id}")
                        self._increment(redis_client)
                        if cr.status_code != 200:
                            continue
                        col_data = cr.json()
                    except Exception as _swallowed:
                        logger.debug(f"[suppressed] {_swallowed}")
                        continue

                    col_text = str(col_data)
                    for hit in sweep_text_for_tokens(col_text, "postman"):
                        tok = hit["token"]
                        if tok in seen_tokens:
                            continue
                        seen_tokens.add(tok)
                        results.append({
                            "token": tok,
                            "meta": {
                                "postman_collection_id": collection_id,
                                "postman_name": col_name,
                                "extracted_from": "collection_body",
                            },
                        })

                    with contextlib.suppress(Exception):
                        redis_client.setex(redis_key, 7 * 86400, "1")

                    await asyncio.sleep(1)
                await asyncio.sleep(2)

        logger.info(f"[Postman] returned {len(results)} matches")
        return results
