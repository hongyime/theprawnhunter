"""Scanner client for UrlScan."""

from ..common import *

class UrlScanService:
    """
    URLScan.io API - Search for pages containing api.telegram.org
    Free tier: 1000 searches/day, 100 results/search
    """
    def __init__(self):
        self.api_key = settings.URLSCAN_KEY
        self.search_url = "https://urlscan.io/api/v1/search/"

    async def search(self, query: str, country_code: str = None) -> list[dict[str, Any]]:
        if not self.api_key:
            logger.warning("    [URLScan] No API key found")
            return []

        try:
            headers = {
                'API-Key': self.api_key,
                'Content-Type': 'application/json'
            }

            # URLScan query format: search in page content
            api_query = f'page.body:"{query}" OR page.url:*{query}*'
            if country_code:
                api_query = f'({api_query}) AND page.country:"{country_code}"'
                logger.info(f"    [URLScan] Adding country filter: {country_code}")

            params = {'q': api_query, 'size': 500}
            logger.info(f"    [URLScan] Searching: {api_query[:50]}...")

            async def do_urlscan():
                async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                    res = await client.get(self.search_url, headers=headers, params=params)
                    if res.status_code in [401, 403]:
                        raise Exception("Invalid URLScan Key")
                    res.raise_for_status()
                    return res.json()

            data = await retry_with_backoff(do_urlscan)
            if not data:
                return []

            results_list = data.get('results', [])
            logger.info(f"    [URLScan] Found {len(results_list)} hits. scanning cache & live...")

            # Filter Logic (Date etc)
            from datetime import datetime, timedelta
            three_hours_ago = datetime.utcnow() - timedelta(hours=3)

            valid_items = []
            for r in results_list:
                try:
                    ts = r.get('task', {}).get('time', '')
                    if ts:
                        scan_time = datetime.fromisoformat(ts.replace('Z', '+00:00').split('+')[0])
                        if scan_time >= three_hours_ago:
                            valid_items.append(r)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")

            # Sort and Cap
            valid_items = sorted(valid_items, key=lambda x: x.get('task', {}).get('time', ''), reverse=True)
            if len(valid_items) > 300:
                valid_items = valid_items[:300]
            elif not valid_items and len(results_list) > 0:
                valid_items = results_list[:50]

            final_results = []

            # Parallel processing of DOM cache and Live Scan
            async with httpx.AsyncClient(verify=False, timeout=10.0) as scan_client:
                tasks = []
                sem = asyncio.Semaphore(20)

                async def process_urlscan_item(item):
                    async with sem:
                        page_url = item.get('page', {}).get('url', '')
                        scan_id = item.get('_id')
                        item_found_tokens = []

                        # 1. Cached DOM Scan
                        if scan_id:
                            try:
                                dom_url = f"https://urlscan.io/dom/{scan_id}"
                                dom_res = await scan_client.get(dom_url, headers=headers)
                                if dom_res.status_code == 200:
                                     content = dom_res.text
                                     tokens = TOKEN_PATTERN.findall(content)
                                     cids = CHAT_ID_PATTERN.findall(content)
                                     cid = cids[0] if cids else None
                                     for t in tokens:
                                         item_found_tokens.append({'token': t, 'chat_id': cid})
                            except Exception as _swallowed:
                                logger.debug(f"[suppressed] {_swallowed}")

                        # 2. Live Deep Scan
                        if page_url:
                            try:
                                # URL Regex
                                url_tokens = TOKEN_PATTERN.findall(page_url)
                                url_cids = CHAT_ID_PATTERN.findall(page_url)
                                url_cid = url_cids[0] if url_cids else None
                                for t in url_tokens:
                                    item_found_tokens.append({'token': t, 'chat_id': url_cid})

                                # Deep Scan
                                live_items = await _perform_active_deep_scan(page_url, client=scan_client)
                                item_found_tokens.extend(live_items)
                            except Exception as _swallowed:
                                logger.debug(f"[suppressed] {_swallowed}")

                        return (item, item_found_tokens)

                for item in valid_items:
                    tasks.append(process_urlscan_item(item))

                # Execute
                task_results = await asyncio.gather(*tasks, return_exceptions=True)

            for res_item in task_results:
                if not res_item or isinstance(res_item, Exception):
                    continue

                item, found = res_item
                # Dedup
                final_map = {}
                for f_item in found:
                    t = f_item['token']
                    c = f_item['chat_id']
                    if t not in final_map or not final_map[t] and c:
                        final_map[t] = c

                for t, cid in final_map.items():
                    if _is_valid_token(t):
                         final_results.append({
                            "token": t,
                            "chat_id": cid,
                            "meta": {
                                "source": "urlscan",
                                "url": item.get('page', {}).get('url', ''),
                                "domain": item.get('page', {}).get('domain'),
                                "scan_id": item.get('_id'),
                                "type": "cached_or_live"
                            }
                        })

            logger.info(f"    [URLScan] Scan complete. {len(final_results)} valid tokens found.")
            return final_results

        except httpx.TimeoutException:
            logger.warning("    [URLScan] Error: Search Request Timed Out")
            return []
        except httpx.RequestError as e:
            logger.error(f"    [URLScan] Network Error: {e}")
            return []
        except Exception as e:
            logger.error(f"    [URLScan] Error: {e}")
            return []
