"""Scanner client for Exa."""

from ..common import *

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
                async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
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
