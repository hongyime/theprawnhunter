"""
Extension scanner services: GithubGistService, GrepAppService, PublicWwwService,
BitbucketService, PastebinService, GoogleSearchService.
Imports shared utilities (TOKEN_PATTERN, _is_valid_token, _perform_active_deep_scan) from scanners.py.
"""
import asyncio
import contextlib
import html as htmlmod
import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.utils.http_client import get_async_http_client

logger = logging.getLogger("scanners")

# GitlabService lives in scanners.py (canonical). Do NOT duplicate here.


class GithubGistService:
    def __init__(self):
        pool = getattr(settings, "GITHUB_TOKENS", None)
        if pool:
            self.token = pool.split(",")[0].strip()
        else:
            self.token = settings.GITHUB_TOKEN
        self.base_url = "https://api.github.com/gists/public"

    async def search(self) -> list[dict[str, Any]]:
        # Gists public endpoint shows recently created gists.
        # We fetch the recent 100 and look for tokens.
        if not self.token:
            logger.warning("    [Gist] Missing GITHUB_TOKEN")
            return []
        try:
            auth_scheme = "Bearer" if self.token.startswith(("ghp_", "github_pat_")) else "token"
            headers = {
                'Authorization': f'{auth_scheme} {self.token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            params = {"per_page": 100}

            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(self.base_url, headers=headers, params=params)
                res.raise_for_status()
                items = res.json()

            results = []
            async with httpx.AsyncClient(verify=False, timeout=10.0) as raw_client:
                tasks = []
                sem = asyncio.Semaphore(10)

                async def process_gist(gist):
                    from app.services.scanners import TOKEN_PATTERN, _is_valid_token
                    async with sem:
                        local_res = []
                        files = gist.get("files", {})
                        for filename, file_data in files.items():
                            raw_url = file_data.get("raw_url")
                            if raw_url:
                                try:
                                    raw_res = await raw_client.get(raw_url)
                                    content = raw_res.text
                                    found = TOKEN_PATTERN.findall(content)
                                    for t in found:
                                        if not _is_valid_token(t): continue
                                        local_res.append({
                                            "token": t,
                                            "meta": {"source": "gist", "gist_id": gist.get("id"), "file": filename}
                                        })
                                except Exception: pass
                        return local_res

                for item in items:
                    tasks.append(process_gist(item))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for batch in scan_results:
                    if batch and isinstance(batch, list):
                        results.extend(batch)

            return results

        except Exception as e:
            logger.error(f"    [Gist] Error: {e}")
            return []


class GrepAppService:
    BOT_TOKEN_RE = re.compile(r"(\d{8,15}:[A-Za-z0-9_-]{35})")

    def __init__(self):
        self.base_url = "https://grep.app/api/search"

    def _strip_html(self, html: str) -> str:
        return htmlmod.unescape(re.sub(r"<[^>]+>", "", html))

    async def search(self) -> list[dict[str, Any]]:
        query = r"api\.telegram\.org/bot\d{8,15}:[A-Za-z0-9_-]{35}"

        try:
            from app.services.scanners import _is_valid_token
            results = []
            seen = set()

            async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
                for page in range(1, 6):
                    params = {"q": query, "regexp": "true", "page": page}
                    res = await client.get(self.base_url, params=params)
                    if res.status_code == 429:
                        logger.warning("    [grep.app] Rate limited, stopping pagination")
                        break
                    res.raise_for_status()
                    data = res.json()

                    hits = data.get("hits", {}).get("hits", [])
                    if not hits:
                        break

                    for hit in hits:
                        snippet = hit.get("content", {}).get("snippet", "")
                        clean = self._strip_html(snippet)
                        for t in self.BOT_TOKEN_RE.findall(clean):
                            if t not in seen and _is_valid_token(t):
                                seen.add(t)
                                results.append({
                                    "token": t,
                                    "meta": {"source": "grep.app", "repo": hit.get("repo")}
                                })

                    await asyncio.sleep(1)

            return results
        except Exception as e:
            logger.error(f"    [grep.app] Error: {e}")
            return []


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

            async with httpx.AsyncClient(timeout=30.0) as client:
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
                        except Exception: return None

                for d in domains:
                    tasks.append(scan_domain(d))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for res_item in scan_results:
                    if not res_item or isinstance(res_item, Exception): continue
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
            async with httpx.AsyncClient(timeout=30.0) as client:
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
                        except Exception: return None

                for item in items:
                    tasks.append(scan_url(item.get("link")))

                scan_results = await asyncio.gather(*tasks, return_exceptions=True)
                for res_item in scan_results:
                    if not res_item or isinstance(res_item, Exception): continue
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


class BitbucketService:
    """
    Bitbucket Cloud code search.
    Auth: API token via Bearer header (app passwords deprecated June 2026).
    The snippets endpoint (410 Gone) and global search are both deprecated.
    We use the workspace code search API: POST /2.0/workspaces/{ws}/search/code
    which launched publicly in late 2024.
    """
    def __init__(self):
        self.token = settings.BITBUCKET_API_TOKEN

    async def search(self) -> list[dict[str, Any]]:
        from app.services.scanners import TOKEN_PATTERN, _is_valid_token

        if not self.token:
            logger.warning("    [Bitbucket] No BITBUCKET_API_TOKEN configured")
            return []

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

        search_terms = [
            "api.telegram.org/bot",
            "TELEGRAM_BOT_TOKEN",
            "bot_token",
            "TG_BOT_TOKEN",
        ]

        results: list[dict[str, Any]] = []

        try:
            # First get the list of workspaces this token has access to
            async with httpx.AsyncClient(timeout=30.0) as client:
                ws_res = await client.get(
                    "https://api.bitbucket.org/2.0/workspaces",
                    headers=headers,
                    params={"pagelen": 50},
                )
                if ws_res.status_code == 401:
                    logger.error("    [Bitbucket] 401 — check BITBUCKET_API_TOKEN and its scopes")
                    return []
                if ws_res.status_code != 200:
                    logger.warning(f"    [Bitbucket] Workspaces fetch failed: {ws_res.status_code}")
                    return []

                workspaces = [
                    ws["slug"] for ws in ws_res.json().get("values", [])
                ]

            if not workspaces:
                logger.warning("    [Bitbucket] No workspaces found for this token")
                return []

            logger.info(f"    [Bitbucket] Searching {len(workspaces)} workspace(s): {workspaces}")

            async with httpx.AsyncClient(timeout=30.0) as client:
                sem = asyncio.Semaphore(5)

                async def search_workspace(ws: str, term: str):
                    async with sem:
                        try:
                            res = await client.get(
                                f"https://api.bitbucket.org/2.0/workspaces/{ws}/search/code",
                                headers=headers,
                                params={"search_query": term, "pagelen": 20},
                            )
                            if res.status_code != 200:
                                return []

                            local_res = []
                            for item in res.json().get("values", []):
                                # Each item has content_matches with line snippets
                                for match in item.get("content_matches", []):
                                    for line in match.get("lines", []):
                                        text = line.get("line", "")
                                        for t in TOKEN_PATTERN.findall(text):
                                            if _is_valid_token(t):
                                                file_path = item.get("file", {}).get("path", "")
                                                local_res.append({
                                                    "token": t,
                                                    "meta": {
                                                        "source": "bitbucket_code",
                                                        "workspace": ws,
                                                        "file": file_path,
                                                        "search_term": term,
                                                    },
                                                })
                            return local_res
                        except Exception as e:
                            logger.debug(f"    [Bitbucket] {ws}/{term}: {e}")
                            return []

                tasks = [
                    search_workspace(ws, term)
                    for ws in workspaces
                    for term in search_terms
                ]
                batches = await asyncio.gather(*tasks, return_exceptions=True)
                for b in batches:
                    if isinstance(b, list):
                        results.extend(b)

        except Exception as e:
            logger.error(f"    [Bitbucket] Error: {e}")

        logger.info(f"    [Bitbucket] Found {len(results)} results")
        return results


class PastebinService:
    def __init__(self):
        self.base_url = "https://scrape.pastebin.com/api_scraping.php"

    async def search(self) -> list[dict[str, Any]]:
        # Pastebin scraping API requires IP whitelist. If it fails, we return []
        try:
            params = {"limit": 100}
            async with httpx.AsyncClient(timeout=30.0) as client:
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
                                if not _is_valid_token(t): continue
                                local_res.append({
                                    "token": t,
                                    "meta": {"source": "pastebin", "paste_key": p.get("key")}
                                })
                            return local_res
                        except Exception: return []

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
                            if not paste_id: continue
                            raw_url = f"https://rentry.co/api/raw/{paste_id}"
                        except Exception:
                            continue

                        # Dedup check
                        seen_key = f"rentry:seen:{paste_id}"
                        try:
                            if redis_client.exists(seen_key): continue
                        except Exception: pass

                        try:
                            raw_res = await client.get(raw_url)
                            if raw_res.status_code != 200: continue

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
                            if not paste_id: continue
                            raw_url = f"https://hastebin.com/raw/{paste_id}"
                        except Exception:
                            continue

                        # Dedup check
                        seen_key = f"hastebin:seen:{paste_id}"
                        try:
                            if redis_client.exists(seen_key): continue
                        except Exception: pass

                        try:
                            raw_res = await client.get(raw_url)
                            if raw_res.status_code != 200: continue

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


class NetlasService:
    """
    Netlas.io — internet-wide host/response search engine.
    Two accounts are rotated automatically. Usage is tracked in Redis to
    stay within daily request limits (50/day acct1, 100/day acct2).

    Netlas query syntax uses Lucene-style fields:
      http.body:"..."   http.headers:"..."   http.status_code:200
      ip:x.x.x.x       port:443             protocol:https
    Each search() call costs 1 search coin per query.
    """

    # Daily limits per account (conservative — leave headroom)
    DAILY_LIMITS = {1: 45, 2: 90}   # slightly under 50/100 to be safe
    REDIS_KEY_PREFIX = "netlas:daily_requests"

    def __init__(self):
        self.keys = []
        if settings.NETLAS_API_KEY_1:
            self.keys.append((1, settings.NETLAS_API_KEY_1))
        if settings.NETLAS_API_KEY_2:
            self.keys.append((2, settings.NETLAS_API_KEY_2))

    def _redis(self):
        import redis as redis_lib
        return redis_lib.from_url(settings.REDIS_URL, decode_responses=True)

    def _today_key(self, account_num: int) -> str:
        from datetime import date
        return f"{self.REDIS_KEY_PREFIX}:{account_num}:{date.today().isoformat()}"

    def _get_usage(self, r, account_num: int) -> int:
        val = r.get(self._today_key(account_num))
        return int(val) if val else 0

    def _increment_usage(self, r, account_num: int):
        key = self._today_key(account_num)
        r.incr(key)
        r.expire(key, 86400 * 2)  # auto-expire after 2 days

    def _pick_account(self, r) -> tuple[int, str] | None:
        """Return (account_num, api_key) for the account with remaining quota, or None."""
        for account_num, api_key in self.keys:
            used = self._get_usage(r, account_num)
            limit = self.DAILY_LIMITS.get(account_num, 45)
            if used < limit:
                return account_num, api_key
        return None

    async def search(self, query: str) -> list[dict[str, Any]]:
        """
        Run a single Netlas response search query.
        Returns list of {token, chat_id, meta} dicts.
        Respects daily limits — returns [] if both accounts exhausted.
        """
        from app.services.scanners import CHAT_ID_PATTERN, TOKEN_PATTERN, _is_valid_token

        if not self.keys:
            logger.warning("    [Netlas] No API keys configured (NETLAS_API_KEY_1 / _2)")
            return []

        try:
            r = self._redis()
            account = self._pick_account(r)
            if account is None:
                logger.warning("    [Netlas] Daily request limit reached for all accounts — skipping")
                return []

            account_num, api_key = account

            # Run in thread — netlas SDK is synchronous
            def _do_search():
                import netlas
                conn = netlas.Netlas(api_key=api_key)
                # page=0 returns first page of results (default page size is 20)
                return conn.query(query=query, datatype="response", page=0)

            data = await asyncio.to_thread(_do_search)
            self._increment_usage(r, account_num)

            used_now = self._get_usage(r, account_num)
            limit = self.DAILY_LIMITS[account_num]
            logger.info(
                f"    [Netlas] Acct#{account_num} used {used_now}/{limit} today | "
                f"query: {query[:60]}..."
            )

            results: list[dict[str, Any]] = []
            for item in (data or {}).get("items", []):
                d = item.get("data", {})
                http = d.get("http", {})
                body = http.get("body", "") or ""
                ip = d.get("ip", "")
                port = d.get("port", "")
                protocol = d.get("protocol", "http")

                tokens = TOKEN_PATTERN.findall(body)
                chat_ids = CHAT_ID_PATTERN.findall(body)
                cid = chat_ids[0] if chat_ids else None

                for t in set(tokens):
                    if _is_valid_token(t):
                        results.append({
                            "token": t,
                            "chat_id": cid,
                            "meta": {
                                "source": "netlas",
                                "ip": ip,
                                "port": port,
                                "protocol": protocol,
                                "query": query,
                                "account": account_num,
                            },
                        })

            return results

        except Exception as e:
            logger.error(f"    [Netlas] Error: {e}")
            return []

    async def get_usage_summary(self) -> dict:
        """Return current daily usage for both accounts."""
        r = self._redis()
        summary = {}
        for account_num, _ in self.keys:
            used = self._get_usage(r, account_num)
            limit = self.DAILY_LIMITS[account_num]
            summary[f"account_{account_num}"] = {
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
            }
        return summary


class ReplitService:
    """
    Replit public search — free, no API key required.
    Searches public repls matching Telegram-related queries, then fetches
    common entry-point files (main.py, bot.py, etc.) and extracts tokens.

    Rate: undocumented; 2s/repl as courtesy.
    """

    SEARCH_URL = "https://replit.com/graphql"
    REPL_RAW_BASE = "https://replit.com"

    QUERIES = [
        "telegram bot token",
        "TELEGRAM_BOT_TOKEN",
        "api.telegram.org",
    ]

    ENTRY_FILES = ("main.py", "index.js", "bot.py", "app.py", ".env", "config.py", "settings.py")

    def __init__(self):
        self.timeout = httpx.Timeout(20.0, connect=10.0)

    async def search(self, query: str = None) -> list[dict[str, Any]]:
        import hashlib

        from app.services.scanners import (
            TOKEN_PATTERN,
            _is_valid_token,
        )
        from app.workers.tasks.flow_tasks import redis_client

        results: list[dict[str, Any]] = []
        queries = [query] if query else self.QUERIES

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as client:
            for q in queries:
                try:
                    # Replit GraphQL search for public repls
                    gql_payload = {
                        "operationName": "ReplSearch",
                        "query": """
                            query ReplSearch($query: String!) {
                                search(query: $query, categories: [Repls]) {
                                    replResults {
                                        results {
                                            items {
                                                ... on Repl {
                                                    slug
                                                    user { username }
                                                    url
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        """,
                        "variables": {"query": q},
                    }
                    r = await client.post(
                        self.SEARCH_URL,
                        json=gql_payload,
                        headers={"Content-Type": "application/json"},
                    )
                    if r.status_code != 200:
                        logger.debug(f"[Replit] q='{q[:30]}' HTTP {r.status_code}")
                        await asyncio.sleep(3)
                        continue

                    data = r.json()
                    items = (
                        ((data.get("data") or {}).get("search") or {})
                        .get("replResults", {})
                        .get("results", {})
                        .get("items", [])
                    )
                except Exception as e:
                    logger.warning(f"[Replit] search failed for '{q[:30]}': {e}")
                    await asyncio.sleep(3)
                    continue

                for repl in items[:30]:
                    user = (repl.get("user") or {}).get("username") or "unknown"
                    slug = repl.get("slug")
                    if not slug:
                        continue
                    repl_url = repl.get("url") or f"/@{user}/{slug}"

                    # Dedupe by repl URL
                    h = hashlib.sha256(f"{user}/{slug}".encode()).hexdigest()[:16]
                    redis_key = f"replit:seen:{h}"
                    try:
                        if redis_client.exists(redis_key):
                            continue
                    except Exception:
                        pass

                    # Fetch common entry-point files
                    for filename in self.ENTRY_FILES:
                        raw_url = f"{self.REPL_RAW_BASE}/@{user}/{slug}/raw/{filename}"
                        try:
                            fr = await client.get(raw_url)
                            if fr.status_code != 200 or not fr.text:
                                continue
                            tokens = set(TOKEN_PATTERN.findall(fr.text))
                            for tok in tokens:
                                if not _is_valid_token(tok):
                                    continue
                                results.append({
                                    "token": tok,
                                    "meta": {
                                        "replit_url": f"{self.REPL_RAW_BASE}{repl_url}",
                                        "replit_file": filename,
                                        "replit_user": user,
                                        "extracted_from": "body",
                                    },
                                })
                        except Exception:
                            pass
                        await asyncio.sleep(1)

                    with contextlib.suppress(Exception):
                        redis_client.setex(redis_key, 7 * 86400, "1")

                    await asyncio.sleep(2)
                await asyncio.sleep(3)

        logger.info(f"[Replit] returned {len(results)} matches")
        return results


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
        except Exception:
            return True

    def _increment(self, redis_client):
        try:
            key = self._today_key()
            redis_client.incr(key)
            redis_client.expire(key, 86400 * 2)
        except Exception:
            pass

    async def search(self, query: str = None) -> list[dict[str, Any]]:
        import hashlib

        from app.services.scanners import TOKEN_PATTERN, _is_valid_token
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
                    except Exception:
                        pass

                    if not self._check_budget(redis_client):
                        break

                    try:
                        cr = await client.get(f"{self.API_URL}/collections/{collection_id}")
                        self._increment(redis_client)
                        if cr.status_code != 200:
                            continue
                        col_data = cr.json()
                    except Exception:
                        continue

                    col_text = str(col_data)
                    tokens = set(TOKEN_PATTERN.findall(col_text))
                    for tok in tokens:
                        if tok in seen_tokens or not _is_valid_token(tok):
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


class SearchcodeService:
    """Searchcode public REST API adapter — queries codesearch_I for Telegram bot token leaks."""

    DEFAULT_QUERIES = ["api.telegram.org/bot", "TELEGRAM_BOT_TOKEN="]

    def __init__(self):
        self.timeout = httpx.Timeout(30.0, connect=10.0)

    async def search(self, query: str = None) -> list[dict[str, Any]]:
        from app.services.scanners import (
            TOKEN_PATTERN,
            _is_valid_token,
            extract_infrastructure_context,
        )

        queries = [query] if query else self.DEFAULT_QUERIES
        results: list[dict[str, Any]] = []
        seen_tokens: set = set()

        async with get_async_http_client(timeout=self.timeout, follow_redirects=True) as client:
            for q in queries:
                try:
                    res = await client.get(
                        "https://searchcode.com/api/codesearch_I/",
                        params={"q": q, "p": 0, "per_page": 100},
                    )
                    if res.status_code != 200:
                        logger.debug(f"[Searchcode] q='{q[:30]}' HTTP {res.status_code}")
                        continue

                    for item in res.json().get("results", []):
                        lines = item.get("lines", {})
                        text_block = "\n".join(str(v) for v in lines.values())
                        source_meta = {
                            "source": "searchcode",
                            "domain": "searchcode.com",
                            "query": q,
                            "repository": item.get("repo") or item.get("repository"),
                            "path": item.get("filename") or item.get("name") or item.get("path"),
                        }
                        for line_text in lines.values():
                            for match in TOKEN_PATTERN.findall(str(line_text)):
                                if match not in seen_tokens and _is_valid_token(match):
                                    seen_tokens.add(match)
                                    meta = dict(source_meta)
                                    infrastructure_context = extract_infrastructure_context(
                                        text_block,
                                        source_meta,
                                        token=match,
                                    )
                                    if infrastructure_context:
                                        meta["infrastructure_context"] = infrastructure_context
                                    results.append({
                                        "token": match,
                                        "meta": meta,
                                    })
                except Exception as e:
                    logger.warning(f"[Searchcode] Error for query '{q[:30]}': {e}")

        logger.info(f"[Searchcode] returned {len(results)} matches")
        return results
