"""Scanner client for GoogleSearch."""

from ..common import *

class GoogleSearchService:
    def __init__(self):
        self.key = settings.GOOGLE_SEARCH_KEY
        self.cse_id = settings.GOOGLE_CSE_ID
        self.base_url = "https://www.googleapis.com/customsearch/v1"

    async def search(self, dork: str = r'site:pastebin.com "api.telegram.org/bot"') -> list[dict[str, Any]]:
        if not self.key or not self.cse_id:
            logger.warning("    [GoogleSearch] Missing GOOGLE_SEARCH_KEY or GOOGLE_CSE_ID")
            return []

        try:
            params = {
                "key": self.key,
                "cx": self.cse_id,
                "q": dork,
                "num": 10
            }
            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                res = await client.get(self.base_url, params=params)
                res.raise_for_status()
                data = res.json()

            results = []
            from app.services.scanners import _perform_active_deep_scan
            items = data.get("items", [])

            async with httpx.AsyncClient(verify=False, timeout=10.0) as scan_client:
                tasks = []
                sem = asyncio.Semaphore(5)

                async def scan_url(link):
                    async with sem:
                        try:
                            found = await _perform_active_deep_scan(link, client=scan_client)
                            return link, found
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                            return None

                for item in items:
                    tasks.append(scan_url(item.get("link")))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for res_item in scan_results:
                    if not res_item or isinstance(res_item, Exception):
                        continue
                    target_url, f_items = res_item
                    for t_item in f_items:
                        results.append({
                            "token": t_item['token'],
                            "chat_id": t_item.get('chat_id'),
                            "meta": {"source": "google_dork", "url": target_url}
                        })
            return results

        except Exception as e:
            logger.error(f"    [GoogleSearch] Error: {e}")
            return []
