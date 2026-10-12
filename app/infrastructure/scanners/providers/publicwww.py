"""Scanner client for PublicWww."""

from ..common import *

class PublicWwwService:
    def __init__(self):
        self.key = settings.PUBLICWWW_KEY
        self.base_url = "https://publicwww.com/websites/"

    async def search(self, query: str = '"api.telegram.org/bot"') -> list[dict[str, Any]]:
        if not self.key:
            logger.warning("    [PublicWWW] Missing PUBLICWWW_KEY")
            return []

        try:
            # PublicWWW format: https://publicwww.com/websites/"query"/?export=csv&key=API_KEY
            url = f"{self.base_url}{query}/"
            params = {"export": "json", "key": self.key, "limit": 100}

            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                res = await client.get(url, params=params)
                if res.status_code == 403:
                    logger.warning("    [PublicWWW] Rate limit or Bad Key")
                    return []
                res.raise_for_status()
                # Returns list of domains
                domains = res.json()

            results = []
            async with httpx.AsyncClient(verify=False, timeout=10.0) as scan_client:
                tasks = []
                sem = asyncio.Semaphore(15)

                async def scan_domain(domain):
                    from app.services.scanners import _perform_active_deep_scan
                    async with sem:
                        target = f"http://{domain}" if not domain.startswith("http") else domain
                        try:
                            items = await _perform_active_deep_scan(target, client=scan_client)
                            return target, items
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                            return None

                for d in domains:
                    tasks.append(scan_domain(d))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for res_item in scan_results:
                    if not res_item or isinstance(res_item, Exception):
                        continue
                    target_url, items = res_item
                    for t_item in items:
                        results.append({
                            "token": t_item['token'],
                            "chat_id": t_item.get('chat_id'),
                            "meta": {"source": "publicwww", "url": target_url}
                        })
            return results
        except Exception as e:
            logger.error(f"    [PublicWWW] Error: {e}")
            return []
