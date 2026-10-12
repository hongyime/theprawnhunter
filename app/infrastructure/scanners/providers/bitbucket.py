"""Scanner client for Bitbucket."""

from ..common import *

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
            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
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

            async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
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
