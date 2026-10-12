"""Scanner client for Rentry."""

from ..common import *

class RentryService:
    """
    Rentry.co — highly utilized by threat actors.
    Searches via Exa to bypass direct scraping blocks, then extracts via raw endpoint.
    """
    def __init__(self):
        from app.services.scanners import ExaService
        self.exa = ExaService()

    async def search(self) -> list[dict[str, Any]]:
        from app.services.scanners import TOKEN_PATTERN, _is_valid_token
        from app.workers.tasks.flow_tasks import redis_client

        results: list[dict[str, Any]] = []
        queries = [
            'site:rentry.co "api.telegram.org/bot"',
            'site:rentry.co "TELEGRAM_BOT_TOKEN"'
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
                        if not url or "rentry.co/" not in url:
                            continue

                        # Convert https://rentry.co/xyz to https://rentry.co/api/raw/xyz
                        try:
                            paste_id = url.split("rentry.co/")[-1].strip("/")
                            if not paste_id:
                                continue
                            raw_url = f"https://rentry.co/api/raw/{paste_id}"
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                            continue

                        # Dedup check
                        seen_key = f"rentry:seen:{paste_id}"
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
                                        "meta": {"source": "rentry", "paste_id": paste_id, "url": url}
                                    })

                            with contextlib.suppress(Exception):
                                redis_client.setex(seen_key, 7 * 86400, "1")
                        except Exception as e:
                            logger.debug(f"    [Rentry] raw fetch failed for {paste_id}: {e}")

                        await asyncio.sleep(1) # Courtesy rate limit

                except Exception as e:
                    logger.warning(f"    [Rentry] Exa search failed: {e}")

        return results
