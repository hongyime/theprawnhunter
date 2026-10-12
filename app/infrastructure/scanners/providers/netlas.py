"""Scanner client for Netlas."""

from ..common import *

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
