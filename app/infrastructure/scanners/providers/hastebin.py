"""Scanner client for Hastebin."""

from ..common import *

class HastebinService:
    """
    Hastebin — developer pastebin often containing .env or console outputs.
    Searches via Exa, fetches raw data from /raw/{id}.
    """
    def __init__(self):
        from app.services.scanners import ExaService
        self.exa = ExaService()

    async def search(self) -> list[dict[str, Any]]:
        from app.services.scanners import TOKEN_PATTERN, _is_valid_token
        from app.workers.tasks.flow_tasks import redis_client

        results: list[dict[str, Any]] = []
        queries = [
            'site:hastebin.com "api.telegram.org/bot"',
            'site:hastebin.com "TELEGRAM_BOT_TOKEN"'
        ]

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for q in queries:
                try:
                    exa_results = await self.exa.search(q)
                    if not exa_results:
                        continue

                    for match in exa_results:
                        meta = match.get("meta", {})
                        url = meta.get("url")
                        if not url or "hastebin.com/" not in url:
                            continue

                        # Convert https://hastebin.com/xyz to https://hastebin.com/raw/xyz
                        try:
                            # Avoid matching hastebin.com/raw/xyz directly if already formatted
                            paste_id = url.split("/")[-1].strip()
                            if not paste_id:
                                continue
                            raw_url = f"https://hastebin.com/raw/{paste_id}"
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                            continue

                        # Dedup check
                        seen_key = f"hastebin:seen:{paste_id}"
                        try:
                            if redis_client.exists(seen_key):
                                continue
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")

                        try:
                            raw_res = await client.get(raw_url)
                            if raw_res.status_code != 200:
                                continue

                            found = TOKEN_PATTERN.findall(raw_res.text)
                            for t in found:
                                if _is_valid_token(t):
                                    results.append({
                                        "token": t,
                                        "meta": {"source": "hastebin", "paste_id": paste_id, "url": url}
                                    })

                            with contextlib.suppress(Exception):
                                redis_client.setex(seen_key, 7 * 86400, "1")
                        except Exception as e:
                            logger.debug(f"    [Hastebin] raw fetch failed for {paste_id}: {e}")

                        await asyncio.sleep(1) # Courtesy rate limit

                except Exception as e:
                    logger.warning(f"    [Hastebin] Exa search failed: {e}")

        return results
