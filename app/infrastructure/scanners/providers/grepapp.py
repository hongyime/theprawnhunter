"""Scanner client for GrepApp."""

from ..common import *

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
