"""Scanner client for Pastebin."""

from ..common import *

class PastebinService:
    def __init__(self):
        self.base_url = "https://scrape.pastebin.com/api_scraping.php"

    async def search(self) -> list[dict[str, Any]]:
        # Pastebin scraping API requires IP whitelist. If it fails, we return []
        try:
            params = {"limit": 100}
            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                res = await client.get(self.base_url, params=params)
                if res.status_code == 403:
                    logger.warning("    [Pastebin] IP not whitelisted for scraping API")
                    return []
                res.raise_for_status()
                pastes = res.json()

            results = []
            async with httpx.AsyncClient(verify=False, timeout=10.0) as raw_client:
                tasks = []
                sem = asyncio.Semaphore(15)

                async def fetch_paste(p):
                    from app.services.scanners import TOKEN_PATTERN, _is_valid_token
                    async with sem:
                        scrape_url = p.get("scrape_url")
                        try:
                            raw_res = await raw_client.get(scrape_url)
                            content = raw_res.text
                            found = TOKEN_PATTERN.findall(content)
                            local_res = []
                            for t in found:
                                if not _is_valid_token(t):
                                    continue
                                local_res.append({
                                    "token": t,
                                    "meta": {"source": "pastebin", "paste_key": p.get("key")}
                                })
                            return local_res
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                            return []

                for p in pastes:
                    tasks.append(fetch_paste(p))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for batch in scan_results:
                    if batch and isinstance(batch, list):
                        results.extend(batch)

            return results
        except Exception as e:
             logger.error(f"    [Pastebin] Error: {e}")
             return []
