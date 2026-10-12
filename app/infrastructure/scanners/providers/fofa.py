"""Scanner client for Fofa."""

from ..common import *

class FofaService:
    def __init__(self):
        self.email = settings.FOFA_EMAIL
        self.key = settings.FOFA_KEY
        self.base_url = "https://fofa.info/api/v1/search/all"

    async def search(self, query: str = 'body="api.telegram.org/bot"', country_code: str = None) -> list[dict[str, Any]]:
        if not (self.email and self.key):
            return []
        try:
            full_query = query
            if country_code:
                full_query = f'{query} && country="{country_code}"'
                logger.info(f"    [FOFA] Adding country filter: {country_code}")

            qbase64 = base64.b64encode(full_query.encode()).decode()
            params = {'email': self.email, 'key': self.key, 'qbase64': qbase64, 'fields': 'host,ip,port', 'size': 100}
            logger.info(f"    [FOFA] Searching: {full_query}")

            async def do_fofa():
                async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                    res = await client.get(self.base_url, params=params)
                    if res.status_code != 200:
                        res.raise_for_status() # Trigger retry on non-200
                    return res.json().get("results", [])

            results_data = await retry_with_backoff(do_fofa)
            if results_data is None:
                return []

            valid_results = []

            # Parallel Active Scan
            async with httpx.AsyncClient(verify=False, timeout=10.0) as scan_client:
                tasks = []
                sem = asyncio.Semaphore(20)

                async def process_row(row):
                    async with sem:
                        host = row[0]
                        # URL Construction
                        target_url = host if host.startswith("http") else (f"https://{host}" if row[2]=="443" else f"http://{host}:{row[2]}")

                        try:
                            # Deep scan
                            items = await _perform_active_deep_scan(target_url, client=scan_client)
                            return (target_url, items)
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                            return None

                for row in results_data:
                    tasks.append(process_row(row))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)

            for item in scan_results:
                if not item or isinstance(item, Exception):
                    continue
                target_url, items = item
                for t_item in items:
                    valid_results.append({
                        "token": t_item['token'],
                        "chat_id": t_item['chat_id'],
                        "meta": {"source": "fofa", "url": target_url}
                    })

            return valid_results
        except httpx.TimeoutException:
            logger.warning("    [FOFA] Error: Request Timed Out")
            return []
        except httpx.RequestError as e:
            logger.error(f"    [FOFA] Network Error: {e}")
            return []
        except Exception as e:
            logger.error(f"    [FOFA] Error: {e}")
            return []
