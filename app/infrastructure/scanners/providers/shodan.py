"""Scanner client for Shodan."""

from ..common import *

class ShodanService:
    def __init__(self):
        self.api_key = settings.SHODAN_KEY
        self.base_url = "https://api.shodan.io/shodan/host/search"

    async def search(self, query: str, country_code: str = None) -> list[dict[str, Any]]:
        if not self.api_key:
            return []
        try:
            full_query = query
            if country_code:
                full_query = f'{query} country:"{country_code}"'
                logger.info(f"    [Shodan] Adding country filter: {country_code}")

            # Shodan API call must be async or threaded. Since it's one call, requests is fine IF wrapped,
            # but ideally use httpx.
            params = {'key': self.api_key, 'query': full_query}

            async def do_search():
                async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                    res = await client.get(self.base_url, params=params)
                    res.raise_for_status()
                    return res.json().get('matches', [])

            matches = await retry_with_backoff(do_search)
            if matches is None:
                return []

            # Sort/Filter
            from datetime import datetime, timedelta
            three_hours_ago = datetime.utcnow() - timedelta(hours=3)
            matches = sorted(matches, key=lambda x: x.get('timestamp', ''), reverse=True)
            recent_matches = []
            for m in matches:
                try:
                    ts = m.get('timestamp', '')
                    if ts:
                        match_time = datetime.fromisoformat(ts.replace('Z', '+00:00').split('+')[0])
                        if match_time >= three_hours_ago:
                            recent_matches.append(m)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")

            matches = recent_matches if len(recent_matches) > len(matches[:300]) else matches[:300]

            results = []
            logger.info(f"    [Shodan] Processing {len(matches)} matches...")

            # CONCURRENT PROCESSING
            async with httpx.AsyncClient(verify=False, timeout=10.0) as scan_client:
                tasks = []
                sem = asyncio.Semaphore(20) # Limit concurrency

                async def process_match(match):
                    async with sem:
                        ip = match.get('ip_str')
                        port = match.get('port')
                        banner = match.get('data', '')

                        # Passive
                        tokens_found = TOKEN_PATTERN.findall(banner)
                        chat_ids_found = CHAT_ID_PATTERN.findall(banner)
                        passive_cid = chat_ids_found[0] if chat_ids_found else None

                        local_found = [{'token': t, 'chat_id': passive_cid} for t in set(tokens_found) if _is_valid_token(t)]

                        # Active Deep Scan
                        if not local_found:
                            try:
                                proto = "https" if port == 443 else "http"
                                target_url = f"{proto}://{ip}:{port}"
                                active_found = await _perform_active_deep_scan(target_url, client=scan_client)
                                local_found.extend(active_found)
                            except Exception as _swallowed:
                                logger.debug(f"[suppressed] {_swallowed}")

                        return (ip, port, local_found)

                for m in matches:
                    tasks.append(process_match(m))

                if tasks:
                    batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    batch_results = []

            # Aggregate Results
            for res_item in batch_results:
                if isinstance(res_item, Exception):
                    continue
                if not isinstance(res_item, tuple):
                    continue # Should be tuple

                ip, port, found_items = res_item

                # Dedup within this match
                seen_t = set()
                for item in found_items:
                    t = item['token']
                    if t in seen_t:
                        continue
                    seen_t.add(t)

                    results.append({
                        "token": t,
                        "chat_id": item['chat_id'],
                        "meta": {
                            "ip": ip,
                            "port": port,
                            "shodan_data": "verified_active" if ip else "banner_match"
                        }
                    })
            return results
        except httpx.TimeoutException:
            logger.warning("    [Shodan] Error: Request Timed Out")
            return []
        except httpx.RequestError as e:
            logger.error(f"    [Shodan] Network Error: {e}")
            return []
        except Exception as e:
            logger.error(f"    [Shodan] Error: {e}")
            return []
