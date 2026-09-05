"""
Primary scanner services: ShodanService, FofaService, UrlScanService, GithubService, GitlabService.
Also exports shared utilities: TOKEN_PATTERN, _is_valid_token, _perform_active_deep_scan.
Complementary services (GithubGistService, GrepAppService, etc.) live in scanners_extension.py.
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import random
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import urllib3

from app.core.config import settings
from app.utils.http_client import get_async_http_client

logger = logging.getLogger("scanners")

# NEW: Resilience Helper
async def retry_with_backoff(func, max_retries=3, initial_delay=2, backoff_factor=2):
    """Exponential backoff decorator for async functions."""
    retries = 0
    delay = initial_delay
    while retries <= max_retries:
        try:
            return await func()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429: # Rate limit
                retry_after = e.response.headers.get("Retry-After")
                wait_time = int(retry_after) if retry_after and retry_after.isdigit() else delay
                logger.warning(f"⚠️ Rate limited. Waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
            elif e.response.status_code in [500, 502, 503, 504]: # Server errors
                logger.warning(f"⚠️ Server error {e.response.status_code}. Retrying in {delay}s...")
                await asyncio.sleep(delay)
            else:
                raise # 400, 401, 403, 404 should probably fail immediately
        except (httpx.RequestError, asyncio.TimeoutError) as e:
            logger.warning(f"⚠️ Network error: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

        retries += 1
        delay *= backoff_factor
    return None # exhausted retries

# Suppress SSL warnings for active scanning of random IPs (self-signed certs etc)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)




# User-provided fingerprint for stealth settings
SPOOFED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",  # Derived from "lang": "en-GB"
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chrome";v="143", "Not=A?Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"'
}

# Regex for Telegram Bot Token: digits:35chars
# Handles bare tokens and /bot{token} URL form (e.g. api.telegram.org/bot123:xxx)
# Negative lookbehind on [A-Za-z0-9] so "mybot12345:..." won't match, but "/bot12345:..." will.
TOKEN_PATTERN = re.compile(r'(?<![A-Za-z0-9])(?:bot)?(\d{8,15}:[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])')
ADJACENT_ENDPOINT_REGEX = re.compile(
    r"(https?://[^\s'\"]+)|"
    r"(?:[a-zA-Z0-9_]+_(?:HOST|DOMAIN|URL|IP|SERVER|API|ENDPOINT|PORT)\s*[:=]\s*[\"']?([^\s'\"]+)[\"']?)",
    re.IGNORECASE,
)
LOCAL_ENDPOINT_VALUES = {"127.0.0.1", "0.0.0.0", "localhost", "::1"}


def _is_valid_token(token_str: str) -> bool:
    """
    Strict validation to filter out Fernet strings, hashes, and junk.
    Valid Telegram token: 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx (digits:35chars)
    """
    try:
        # Explicit Fernet rejection (starts with gAAAA)
        if token_str.startswith("gAAAA"):
            return False

        # Must contain exactly one colon
        if ":" not in token_str:
            return False
        if token_str.count(":") != 1:
            return False

        parts = token_str.split(":", 1)
        bot_id, secret = parts

        # Bot ID must be 8-15 digits, no leading zeros
        if not bot_id.isdigit():
            return False
        if len(bot_id) < 8 or len(bot_id) > 15:
            return False
        if len(bot_id) > 1 and bot_id.startswith("0"):
            return False

        # Secret must be exactly 35 characters
        if len(secret) != 35:
            return False

        # Secret must only contain allowed chars (base64-ish)
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        if not all(c in allowed for c in secret):
            return False

        # Suspicious: Pure hex (likely hash collision)
        is_pure_hex = all(c in "0123456789abcdefABCDEF" for c in secret)
        if is_pure_hex:
            # Real tokens have mixed case and special chars, pure hex is suspicious
            return False

        return True
    except Exception:
        return False


def _token_context(code_text: str, token: str, line_radius: int = 50) -> str:
    lines = (code_text or "").splitlines()
    if not lines:
        return code_text or ""
    for idx, line in enumerate(lines):
        if token in line:
            start = max(0, idx - line_radius)
            end = min(len(lines), idx + line_radius + 1)
            return "\n".join(lines[start:end])
    return code_text or ""


def _is_remote_endpoint(value: str) -> bool:
    if not value:
        return False
    clean = value.strip().strip("',\")]}>,")
    if not clean:
        return False
    parsed = urlparse(clean if "://" in clean else f"//{clean}")
    host = parsed.hostname or clean.split("/")[0].split(":")[0]
    if host.lower() in LOCAL_ENDPOINT_VALUES:
        return False
    return "." in host or host.replace(".", "").isdigit() or "/" in clean


def extract_infrastructure_context(
    code_text: str,
    source_meta: dict[str, Any],
    token: str | None = None,
) -> dict[str, Any]:
    context_text = _token_context(code_text, token) if token else (code_text or "")
    endpoints = set()
    for url_match, config_match in ADJACENT_ENDPOINT_REGEX.findall(context_text):
        candidate = (url_match or config_match or "").strip().strip("',\")]}>,")
        if _is_remote_endpoint(candidate):
            endpoints.add(candidate)

    if not endpoints:
        return {}

    return {
        "source_file_path": source_meta.get("path") or source_meta.get("file") or source_meta.get("filename"),
        "repository_uri": source_meta.get("repository") or source_meta.get("repo") or source_meta.get("project_id"),
        "co_located_endpoints": sorted(endpoints),
        "extracted_at": datetime.now(UTC).isoformat(),
    }

# Strict Regex: \b (boundary) + digits + : + 35 chars + \b
# Regex for extracting chat_id (e.g., chat_id=12345 or "chat_id": 12345)
CHAT_ID_PATTERN = re.compile(r'(?:chat_id|chat|target|cid)[=_":\s]+([-\d]+)', re.IGNORECASE)

async def _perform_active_deep_scan(target_url: str, client: httpx.AsyncClient = None) -> list[dict[str, str]]:
    """
    Connects to a URL, scans HTML body, finds <script src="..."> tags,
    fetches those JS files, and scans them too.
    Returns a list of dicts: [{'token': '...', 'chat_id': '...'}]
    """
    found_results = [] # List of {'token': ..., 'chat_id': ...}

    def extract_from_text(text: str) -> list[dict[str, str]]:
        tokens = TOKEN_PATTERN.findall(text)
        chat_ids = CHAT_ID_PATTERN.findall(text)

        extracted = []
        if len(text) < 500:
             cid = chat_ids[0] if chat_ids else None
             for t in tokens:
                 if _is_valid_token(t):
                     extracted.append({'token': t, 'chat_id': cid})
             return extracted

        for t in set(tokens): # dedup tokens
             if _is_valid_token(t):
                 cid = chat_ids[0] if chat_ids else None
                 extracted.append({'token': t, 'chat_id': cid})
        return extracted

    # Use provided client or create a temporary one
    should_close = False
    if client is None:
        client = httpx.AsyncClient(verify=False, timeout=10.0)
        should_close = True

    try:
        # 0. Check URL string itself
        found_results.extend(extract_from_text(target_url))

        if "api.telegram.org" in target_url:
            if should_close: await client.aclose()
            return found_results

        # 1. Fetch Main HTML
        # print(f"      [DeepScan] Fetching: {target_url}")
        try:
            res = await client.get(target_url, headers=SPOOFED_HEADERS, follow_redirects=True)
            if res.status_code == 200:
                html_content = res.text
                found_results.extend(extract_from_text(html_content))

                # 2. Find External JS
                js_links = re.findall(r'src=["\'](.*?.js)["\']', html_content)
                unique_js = list(set(js_links))[:5]

                # Create async tasks for JS fetching
                js_tasks = []
                for js_path in unique_js:
                    if js_path.startswith("//"): js_url = "https:" + js_path
                    elif js_path.startswith("http"): js_url = js_path
                    else:
                        from urllib.parse import urljoin
                        js_url = urljoin(target_url, js_path)

                    js_tasks.append(client.get(js_url, headers=SPOOFED_HEADERS, follow_redirects=True))

                if js_tasks:
                    js_responses = await asyncio.gather(*js_tasks, return_exceptions=True)
                    for js_res in js_responses:
                            if isinstance(js_res, httpx.Response) and js_res.status_code == 200:
                                found_results.extend(extract_from_text(js_res.text))

        except httpx.TimeoutException:
            # print(f"      [DeepScan] Timeout: {target_url}")
            pass
        except httpx.ConnectError:
            pass
        except httpx.HTTPStatusError:
            pass
        except Exception:
            pass

        # Deduplicate
        final_map = {}
        for item in found_results:
            t = item['token']
            c = item['chat_id']
            if t not in final_map:
                final_map[t] = c
            elif not final_map[t] and c:
                final_map[t] = c

        return [{'token': t, 'chat_id': c} for t, c in final_map.items()]

    except Exception:
        return []
    finally:
        if should_close:
            await client.aclose()


class ShodanService:
    def __init__(self):
        self.api_key = settings.SHODAN_KEY
        self.base_url = "https://api.shodan.io/shodan/host/search"

    async def search(self, query: str, country_code: str = None) -> list[dict[str, Any]]:
        if not self.api_key: return []
        try:
            full_query = query
            if country_code:
                full_query = f'{query} country:"{country_code}"'
                logger.info(f"    [Shodan] Adding country filter: {country_code}")

            # Shodan API call must be async or threaded. Since it's one call, requests is fine IF wrapped,
            # but ideally use httpx.
            params = {'key': self.api_key, 'query': full_query}

            async def do_search():
                async with httpx.AsyncClient(timeout=30.0) as client:
                    res = await client.get(self.base_url, params=params)
                    res.raise_for_status()
                    return res.json().get('matches', [])

            matches = await retry_with_backoff(do_search)
            if matches is None: return []

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
                        if match_time >= three_hours_ago: recent_matches.append(m)
                except Exception: pass

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
                            except Exception: pass

                        return (ip, port, local_found)

                for m in matches:
                    tasks.append(process_match(m))

                if tasks:
                    batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    batch_results = []

            # Aggregate Results
            for res_item in batch_results:
                if isinstance(res_item, Exception): continue
                if not isinstance(res_item, tuple): continue # Should be tuple

                ip, port, found_items = res_item

                # Dedup within this match
                seen_t = set()
                for item in found_items:
                    t = item['token']
                    if t in seen_t: continue
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

class FofaService:
    def __init__(self):
        self.email = settings.FOFA_EMAIL
        self.key = settings.FOFA_KEY
        self.base_url = "https://fofa.info/api/v1/search/all"

    async def search(self, query: str = 'body="api.telegram.org/bot"', country_code: str = None) -> list[dict[str, Any]]:
        if not (self.email and self.key): return []
        try:
            full_query = query
            if country_code:
                full_query = f'{query} && country="{country_code}"'
                logger.info(f"    [FOFA] Adding country filter: {country_code}")

            qbase64 = base64.b64encode(full_query.encode()).decode()
            params = {'email': self.email, 'key': self.key, 'qbase64': qbase64, 'fields': 'host,ip,port', 'size': 100}
            logger.info(f"    [FOFA] Searching: {full_query}")

            async def do_fofa():
                async with httpx.AsyncClient(timeout=30.0) as client:
                    res = await client.get(self.base_url, params=params)
                    if res.status_code != 200:
                        res.raise_for_status() # Trigger retry on non-200
                    return res.json().get("results", [])

            results_data = await retry_with_backoff(do_fofa)
            if results_data is None: return []

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
                        except Exception:
                            return None

                for row in results_data:
                    tasks.append(process_row(row))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)

            for item in scan_results:
                if not item or isinstance(item, Exception): continue
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
                async with httpx.AsyncClient(timeout=30.0) as client:
                    res = await client.get(self.search_url, headers=headers, params=params)
                    if res.status_code in [401, 403]: raise Exception("Invalid URLScan Key")
                    res.raise_for_status()
                    return res.json()

            data = await retry_with_backoff(do_urlscan)
            if not data: return []

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
                except Exception: pass

            # Sort and Cap
            valid_items = sorted(valid_items, key=lambda x: x.get('task', {}).get('time', ''), reverse=True)
            if len(valid_items) > 300: valid_items = valid_items[:300]
            elif not valid_items and len(results_list) > 0: valid_items = results_list[:50]

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
                            except Exception: pass

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
                            except Exception: pass

                        return (item, item_found_tokens)

                for item in valid_items:
                    tasks.append(process_urlscan_item(item))

                # Execute
                task_results = await asyncio.gather(*tasks, return_exceptions=True)

            for res_item in task_results:
                if not res_item or isinstance(res_item, Exception): continue

                item, found = res_item
                # Dedup
                final_map = {}
                for f_item in found:
                    t = f_item['token']
                    c = f_item['chat_id']
                    if t not in final_map:
                        final_map[t] = c
                    elif not final_map[t] and c:
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

class GithubService:
    def __init__(self):
        self.token = settings.GITHUB_TOKEN  # backwards-compat fallback
        self.base_url = "https://api.github.com/search/code"

    def _get_token(self) -> str:
        """
        Round-robin token selection from GITHUB_TOKENS env var (comma-separated).
        Falls back to single GITHUB_TOKEN if pool not configured.

        Uses Redis INCR for distributed round-robin so multiple worker
        processes share the rotation evenly. If Redis is unreachable, falls
        back to random.choice.
        """
        pool = getattr(settings, "GITHUB_TOKENS", None)
        if not pool:
            return self.token or ""
        tokens = [t.strip() for t in pool.split(",") if t.strip()]
        if not tokens:
            return self.token or ""
        if len(tokens) == 1:
            return tokens[0]
        try:
            from app.workers.tasks.flow_tasks import redis_client
            idx = int(redis_client.incr("github_token_rotation")) % len(tokens)
            return tokens[idx]
        except Exception:
            import random as _r
            return _r.choice(tokens)

    async def search(self, query: str) -> list[dict[str, Any]]:
        token = self._get_token()
        if not token:
            logger.warning("GitHub Token missing (set GITHUB_TOKEN or GITHUB_TOKENS)")
            return []

        try:
            # Support both classic (ghp_) and fine-grained (github_pat_) tokens
            auth_scheme = "Bearer" if token.startswith(("ghp_", "github_pat_")) else "token"
            headers = {
                'Authorization': f'{auth_scheme} {token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            params = {'q': query, 'per_page': 100, 'sort': 'indexed', 'order': 'desc'}

            async def do_github():
                async with httpx.AsyncClient(timeout=30.0) as client:
                    res = await client.get(self.base_url, headers=headers, params=params)
                    if res.status_code in [403, 429]:
                        # Check Rate Limit sleep
                        raise httpx.HTTPStatusError("Rate Limit", request=res.request, response=res)
                    res.raise_for_status()
                    return res.json().get('items', [])

            items = await retry_with_backoff(do_github, max_retries=2)
            if not items: items = []

            # Parallel Raw Fetching
            results = []
            async with httpx.AsyncClient(verify=False, timeout=10.0) as raw_client:
                tasks = []
                sem = asyncio.Semaphore(10) # GitHub raw is sensitive to speed?

                async def fetch_raw(item):
                    async with sem:
                        html_url = item.get('html_url', '')
                        # Convert github.com blob URL to raw.githubusercontent.com
                        raw_url = (
                            html_url
                            .replace('https://github.com', 'https://raw.githubusercontent.com')
                            .replace('/blob/', '/')
                        )
                        # Fallback: use the API to get raw content if URL conversion looks wrong
                        if '/blob/' not in html_url and 'raw.githubusercontent.com' not in raw_url:
                            # Try the GitHub contents API instead
                            repo = item.get('repository', {}).get('full_name', '')
                            path = item.get('path', '')
                            ref = item.get('sha', 'HEAD')
                            raw_url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"

                        try:
                            # Include auth to avoid 60/hr unauthenticated rate limit.
                            # Use the same rotated token as the search call so each
                            # request hits a different bucket — raw.githubusercontent
                            # has its own per-token rate limit separate from search.
                            raw_res = await raw_client.get(raw_url, headers={
                                'Authorization': f'{auth_scheme} {token}'
                            })
                            if raw_res.status_code == 404:
                                return []
                            content = raw_res.text
                            found = TOKEN_PATTERN.findall(content)
                            local_res = []
                            source_meta = {
                                "source": "github",
                                "repository": item.get('repository', {}).get('full_name'),
                                "repo": item.get('repository', {}).get('full_name'),
                                "path": item.get('path'),
                                "file_url": item.get('html_url'),
                            }
                            for t in found:
                                if not _is_valid_token(t): continue
                                meta = dict(source_meta)
                                infrastructure_context = extract_infrastructure_context(
                                    content,
                                    source_meta,
                                    token=t,
                                )
                                if infrastructure_context:
                                    meta["infrastructure_context"] = infrastructure_context
                                local_res.append({
                                    "token": t,
                                    "meta": meta
                                })
                            return local_res
                        except Exception:
                            return []

                for item in items:
                    tasks.append(fetch_raw(item))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)

                for batch in scan_results:
                    if batch and isinstance(batch, list):
                        results.extend(batch)
            return results
        except httpx.TimeoutException:
            logger.warning("    [GitHub] Error: Request Timed Out")
            return []
        except httpx.RequestError as e:
            logger.error(f"    [GitHub] Network Error: {e}")
            return []
        except Exception as e:
            logger.error(f"    [GitHub] Error: {e}")
            return []

# CensysService REMOVED - Free tier doesn't have search API access

# HybridAnalysisService REMOVED - API key permission issues with /search/terms endpoint


class GitlabService:
    def __init__(self):
        self.token = settings.GITLAB_TOKEN
        self.base_url = "https://gitlab.com/api/v4/search"

    async def search(self, query: str = "api.telegram.org/bot") -> list[dict[str, Any]]:
        if not self.token:
            logger.warning("    [GitLab] Missing GITLAB_TOKEN")
            return []

        try:
            headers = {"PRIVATE-TOKEN": self.token}
            params = {"scope": "blobs", "search": query}

            async def do_gitlab_search():
                async with httpx.AsyncClient(timeout=30.0) as client:
                    res = await client.get(self.base_url, headers=headers, params=params)
                    res.raise_for_status()
                    return res.json()

            items = await retry_with_backoff(do_gitlab_search)
            if not items: items = []

            # GitLab blobs api returns project_id and filename.
            # We must fetch the raw blob or the file content via projects API.
            # Due to GitLab API structures, getting raw content is complex in search scopes.
            # Instead, we will simulate scraping the blob URL directly or doing a shallow parse of the match data.
            results = []

            async with httpx.AsyncClient(verify=False, timeout=10.0) as raw_client:
                tasks = []
                sem = asyncio.Semaphore(10)

                async def fetch_raw(item):
                    from app.services.scanners import TOKEN_PATTERN, _is_valid_token
                    async with sem:
                        project_id = item.get("project_id")
                        filename = item.get("filename")
                        ref = item.get("ref", "master")

                        raw_url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/files/{filename}/raw?ref={ref}"
                        try:
                            raw_res = await raw_client.get(raw_url, headers=headers)
                            content = raw_res.text
                            found = TOKEN_PATTERN.findall(content)
                            local_res = []
                            for t in found:
                                if not _is_valid_token(t): continue
                                local_res.append({
                                    "token": t,
                                    "meta": {"source": "gitlab", "project_id": project_id, "file": filename}
                                })
                            return local_res
                        except Exception:
                            return []

                for item in items:
                    tasks.append(fetch_raw(item))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for batch in scan_results:
                    if batch and isinstance(batch, list):
                        results.extend(batch)

            return results

        except Exception as e:
            logger.error(f"    [GitLab] Error: {e}")
            return []


class ExaService:
    """
    Paste-site crawler using Exa neural search API.
    Replaces SerperService — Exa returns full page content directly,
    eliminating the second HTTP fetch pass that Serper required.

    Scopes searches to known paste/code-sharing domains and extracts
    Telegram bot tokens from returned content inline.
    """
    PASTE_DOMAINS = [
        "pastebin.com",
        "hastebin.com",
        "rentry.co",
        "ghostbin.com",
        "paste.ee",
        "controlc.com",
    ]

    def __init__(self):
        self.base_url = "https://api.exa.ai/search"

    def _get_api_key(self) -> str | None:
        """Pick a random available Exa key from EXA_API_KEY / EXA_API_KEY_2 / EXA_API_KEY_3."""
        keys = [
            key
            for key in (
                settings.EXA_API_KEY,
                getattr(settings, "EXA_API_KEY_2", None),
                getattr(settings, "EXA_API_KEY_3", None),
            )
            if key
        ]
        return random.choice(keys) if keys else None

    async def search(self, query: str = '"api.telegram.org/bot"') -> list[dict[str, Any]]:
        api_key = self._get_api_key()
        if not api_key:
            logger.warning("    [Exa] No EXA_API_KEY configured")
            return []

        try:
            headers = {
                "x-api-key": api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "query": query,
                "numResults": 25,
                "includeDomains": self.PASTE_DOMAINS,
                "contents": {
                    "text": {"maxCharacters": 10000},
                },
                "type": "keyword",
            }

            async def do_exa():
                async with httpx.AsyncClient(timeout=30.0) as client:
                    res = await client.post(self.base_url, headers=headers, json=payload)
                    if res.status_code in [401, 403]:
                        raise httpx.HTTPStatusError(
                            f"Exa auth error {res.status_code}",
                            request=res.request, response=res
                        )
                    res.raise_for_status()
                    return res.json().get("results", [])

            exa_results = await retry_with_backoff(do_exa)
            if not exa_results:
                return []

            results = []
            for item in exa_results:
                text = item.get("text") or ""
                url  = item.get("url", "")
                if not text:
                    continue
                # Extract tokens from returned content — no second fetch needed
                found_tokens = TOKEN_PATTERN.findall(text)
                for t in found_tokens:
                    if not _is_valid_token(t):
                        continue
                    results.append({
                        "token":   t,
                        "chat_id": None,
                        "meta":    {"source": "exa", "url": url},
                    })
            return results

        except Exception as e:
            logger.error(f"    [Exa] Error: {e}")
            return []



class WaybackService:
    """
    Internet Archive Wayback Machine — historical URL scanner.

    Free, no API key. Uses CDX API for URL discovery + archived content fetch.
    Rate limit: ~1 req/sec courtesy (no documented hard cap; we sleep 1.2s).

    Token extraction strategy:
      1. From URL itself — many leaks are `.../bot<TOKEN>/sendMessage?...`
      2. From archived response body — paste content preserved in archive

    Dedup: SHA256 of original_url, cached in Redis 7 days. Avoids re-fetching
    the same snapshot across runs (CDX returns stable URLs across queries).

    Cost profile: 500 snapshots/run × ~2 HTTP calls each = 1000 archive.org
    requests/run, paced at 1.2s = ~20 min run time. Schedule once daily at
    04:00 UTC during quiet period.
    """

    CDX_URL = "https://web.archive.org/cdx/search/cdx"
    ARCHIVE_URL_TEMPLATE = "https://web.archive.org/web/{timestamp}/{url}"

    def __init__(self):
        self.timeout = httpx.Timeout(20.0, connect=10.0)
        self.dedupe_ttl = 7 * 86400  # 7 days

    async def search(self, query_pattern: str = "api.telegram.org", limit: int = 500) -> list[dict[str, Any]]:
        """Query CDX, fetch unseen archived content, extract tokens."""
        from app.workers.tasks.flow_tasks import redis_client
        results: list[dict[str, Any]] = []

        # Step 1: CDX query
        # matchType=domain searches the domain + all subpaths — required for
        # api.telegram.org/bot* style URLs since `prefix` matchType doesn't
        # honor wildcards in the path (only in the host portion).
        params = {
            "url": query_pattern,
            "matchType": "domain",
            "output": "json",
            "limit": limit,
            "filter": ["statuscode:200", "urlkey:.*bot.*"],
        }

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                resp = await client.get(self.CDX_URL, params=params)
                resp.raise_for_status()
                rows = resp.json()
            except Exception as e:
                logger.error(f"    [Wayback] CDX query failed: {e}")
                return []

            if not rows or len(rows) < 2:
                logger.info("    [Wayback] CDX returned 0 rows")
                return []

            # First row is header
            header = rows[0]
            try:
                ts_idx = header.index("timestamp")
                url_idx = header.index("original")
            except ValueError:
                logger.error(f"    [Wayback] Unexpected CDX header: {header}")
                return []

            seen_in_run = set()
            for row in rows[1:]:
                if len(row) < max(ts_idx, url_idx) + 1:
                    continue
                timestamp = row[ts_idx]
                original = row[url_idx]

                # Dedupe by URL hash (multiple snapshots of same URL = redundant)
                url_hash = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:16]
                if url_hash in seen_in_run:
                    continue
                seen_in_run.add(url_hash)

                redis_key = f"wayback:seen:{url_hash}"
                try:
                    if redis_client.exists(redis_key):
                        continue
                except Exception:
                    pass  # Redis down — process anyway, we'll skip the marker

                # Step 2: Extract token from URL itself
                url_tokens = TOKEN_PATTERN.findall(original)
                for tok in url_tokens:
                    if not _is_valid_token(tok):
                        continue
                    results.append({
                        "token": tok,
                        "meta": {
                            "wayback_url": original,
                            "wayback_timestamp": timestamp,
                            "extracted_from": "url",
                        }
                    })

                # Step 3: Fetch archived body (may have additional tokens)
                archive_url = self.ARCHIVE_URL_TEMPLATE.format(timestamp=timestamp, url=original)
                try:
                    arc_resp = await client.get(archive_url)
                    if arc_resp.status_code == 200:
                        body_tokens = set(TOKEN_PATTERN.findall(arc_resp.text))
                        for tok in body_tokens:
                            if tok in url_tokens:
                                continue  # already added via URL extraction
                            if not _is_valid_token(tok):
                                continue
                            results.append({
                                "token": tok,
                                "meta": {
                                    "wayback_url": original,
                                    "wayback_timestamp": timestamp,
                                    "extracted_from": "body",
                                }
                            })
                except Exception as e:
                    logger.debug(f"    [Wayback] Fetch failed {original[:60]}: {e}")

                # Mark seen (1-week dedup)
                with contextlib.suppress(Exception):
                    redis_client.setex(redis_key, self.dedupe_ttl, "1")

                # Courtesy rate limit
                await asyncio.sleep(1.2)

        logger.info(f"    [Wayback] Returned {len(results)} matches across {len(seen_in_run)} snapshots")
        return results


class CommonCrawlService:
    """
    Common Crawl Index API — free historical web crawl URL search.

    Endpoint pattern:
        https://index.commoncrawl.org/CC-MAIN-{crawl_id}-index
        ?url=api.telegram.org&matchType=domain&output=json&limit=N

    Response format: NDJSON (one JSON object per line, NOT a JSON array).

    Strategy:
      1. Discover the latest crawl ID from /collinfo.json
      2. Query that crawl's index for URLs under api.telegram.org domain
      3. Tokens leak via URL query strings: api.telegram.org/bot{TOKEN}/...
      4. Extract tokens directly from URL — no WARC fetch needed for the
         common case (CDX URL field already contains everything we need).

    Cost: zero. No AWS, no Athena, no API key.
    Rate: ~1 req/sec courtesy (no documented hard cap).
    """

    COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
    QUERY_DOMAIN = "api.telegram.org"

    def __init__(self):
        self.timeout = httpx.Timeout(30.0, connect=10.0)
        self.dedupe_ttl = 30 * 86400  # 30 days

    async def search(self, limit: int = 500) -> list[dict[str, Any]]:
        from app.core.redis_srv import redis_srv as _redis
        results: list[dict[str, Any]] = []

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "TheprawnHunter/1.0 (+research)"},
        ) as client:
            # Step 1: discover the newest crawl
            try:
                cr = await client.get(self.COLLINFO_URL)
                cr.raise_for_status()
                colls = cr.json()
                if not colls:
                    logger.warning("[CommonCrawl] empty collinfo")
                    return []
                latest = colls[0]  # newest first
                index_api = latest.get("cdx-api")
                crawl_id = latest.get("id")
                if not index_api:
                    logger.warning(f"[CommonCrawl] no cdx-api for {crawl_id}")
                    return []
                logger.info(f"[CommonCrawl] using crawl {crawl_id}")
            except Exception as e:
                logger.error(f"[CommonCrawl] collinfo failed: {e}")
                return []

            # Step 2: query the index — domain match, paginated by limit
            try:
                idx_resp = await client.get(
                    index_api,
                    params={
                        "url": self.QUERY_DOMAIN,
                        "matchType": "domain",
                        "output": "json",
                        "limit": limit,
                        "filter": "=status:200",  # successful captures only
                    },
                )
                if idx_resp.status_code != 200:
                    logger.warning(f"[CommonCrawl] index HTTP {idx_resp.status_code}")
                    return []
                # NDJSON — one JSON object per line
                raw = idx_resp.text or ""
                lines = [l for l in raw.split("\n") if l.strip()]
            except Exception as e:
                logger.error(f"[CommonCrawl] index query failed: {e}")
                return []

            seen_in_run = set()
            for line in lines:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                url = rec.get("url")
                if not url:
                    continue

                url_hash = hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:16]
                if url_hash in seen_in_run:
                    continue
                seen_in_run.add(url_hash)

                redis_key = f"commoncrawl:seen:{url_hash}"
                try:
                    if _redis.client.exists(redis_key):
                        continue
                except Exception:
                    pass

                # Extract tokens directly from URL
                url_tokens = TOKEN_PATTERN.findall(url)
                for tok in url_tokens:
                    if not _is_valid_token(tok):
                        continue
                    results.append({
                        "token": tok,
                        "meta": {
                            "commoncrawl_url": url,
                            "commoncrawl_timestamp": rec.get("timestamp"),
                            "commoncrawl_crawl": crawl_id,
                            "extracted_from": "url",
                            "source_kind": "commoncrawl",
                        }
                    })

                # Mark seen regardless of match (avoids re-processing same URL)
                with contextlib.suppress(Exception):
                    _redis.client.setex(redis_key, self.dedupe_ttl, "1")

        logger.info(
            f"    [CommonCrawl] returned {len(results)} matches "
            f"across {len(seen_in_run)} URLs"
        )
        return results


class SourcegraphService:
    """
    Sourcegraph public code search — free, no auth, no key.

    Endpoint: https://sourcegraph.com/.api/search/stream
    Format: Server-Sent Events (SSE). Stream lines like 'event: matches\ndata: [...]\n\n'.
    Index size: ~91k repos containing 'api.telegram.org'.
    Rate limit: undocumented; we use 5s between queries as courtesy.

    Replaces the abandoned Replit attempt — Replit's GraphQL now requires
    persisted query hashes (anti-scraping), Sourcegraph remains open.
    """

    STREAM_URL = "https://sourcegraph.com/.api/search/stream"

    QUERIES = [
        "api.telegram.org/bot count:200",
        "TELEGRAM_BOT_TOKEN count:200",
        "TELEGRAM_TOKEN= count:200",
    ]

    def __init__(self):
        self.timeout = httpx.Timeout(45.0, connect=10.0)
        self.dedupe_ttl = 14 * 86400  # 14 days

    async def search(self, query: str = None) -> list[dict[str, Any]]:
        from app.core.redis_srv import redis_srv as _redis
        results: list[dict[str, Any]] = []
        queries = [query] if query else list(self.QUERIES)

        async with get_async_http_client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "TheprawnHunter/1.0 (+research)"},
        ) as client:
            for q in queries:
                try:
                    # SSE stream: query the stream endpoint, parse 'data:' lines
                    r = await client.get(self.STREAM_URL, params={"q": q})
                    if r.status_code != 200:
                        logger.debug(f"[Sourcegraph] q='{q[:30]}' HTTP {r.status_code}")
                        continue
                    body = r.text or ""
                except Exception as e:
                    logger.warning(f"[Sourcegraph] stream failed: {e}")
                    continue

                # SSE parsing — events come as 'event: <type>\ndata: <json>\n\n'
                for chunk in body.split("\n\n"):
                    lines = chunk.split("\n")
                    event_type = None
                    data_str = None
                    for line in lines:
                        if line.startswith("event: "):
                            event_type = line[7:].strip()
                        elif line.startswith("data: "):
                            data_str = line[6:].strip()
                    if event_type != "matches" or not data_str:
                        continue
                    try:
                        matches = json.loads(data_str)
                    except Exception:
                        continue
                    if not isinstance(matches, list):
                        continue

                    for m in matches:
                        if m.get("type") != "content":
                            continue
                        repo = m.get("repository") or ""
                        path = m.get("path") or ""
                        commit = m.get("commit") or ""
                        # repo format: "github.com/owner/name" → strip "github.com/"
                        repo_full = repo.replace("github.com/", "", 1) if repo.startswith("github.com/") else repo

                        # Dedup per (repo, path, commit) — stable identity
                        h = hashlib.sha256(f"{repo}|{path}|{commit}".encode()).hexdigest()[:16]
                        redis_key = f"sourcegraph:seen:{h}"
                        try:
                            if _redis.client.exists(redis_key):
                                continue
                        except Exception:
                            pass

                        # Aggregate matched line text
                        line_matches = m.get("lineMatches") or []
                        for lm in line_matches:
                            line_text = lm.get("line") or ""
                            tokens = set(TOKEN_PATTERN.findall(line_text))
                            for tok in tokens:
                                if not _is_valid_token(tok):
                                    continue
                                results.append({
                                    "token": tok,
                                    "meta": {
                                        "repo": repo_full,
                                        "path": path,
                                        "commit": commit,
                                        "line_number": lm.get("lineNumber"),
                                        "source_kind": "sourcegraph",
                                    }
                                })

                        with contextlib.suppress(Exception):
                            _redis.client.setex(redis_key, self.dedupe_ttl, "1")

                await asyncio.sleep(5)  # courtesy between queries

        logger.info(f"    [Sourcegraph] returned {len(results)} matches")
        return results
