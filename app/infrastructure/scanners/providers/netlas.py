"""Scanner client for Netlas."""

from app.domain.tokens import sweep_text_for_tokens

from ..common import *


class NetlasProviderError(Exception):
    """A Netlas request failed and should be reported by its scanner task."""


class NetlasMissingApiKey(NetlasProviderError):
    """No Netlas account key is configured."""


class NetlasRejectedApiKey(NetlasProviderError):
    """Netlas rejected the configured account key."""


class NetlasRateLimitError(NetlasProviderError):
    """Netlas asked the scanner to wait before making another request."""

    def __init__(self, retry_after: int):
        self.retry_after = max(0, int(retry_after or 0))
        super().__init__(f"Netlas rate limit reached; wait {self.retry_after} seconds")


class NetlasDailyLimitReached(NetlasProviderError):
    """All configured Netlas accounts have used their daily request allowance."""


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
    MAX_REQUESTS_PER_RUN = 45
    RESULTS_PER_REQUEST = 20
    REDIS_KEY_PREFIX = "netlas:daily_requests"

    def __init__(self):
        self.keys = []
        if settings.NETLAS_API_KEY_1:
            self.keys.append((1, settings.NETLAS_API_KEY_1))
        if settings.NETLAS_API_KEY_2:
            self.keys.append((2, settings.NETLAS_API_KEY_2))

    @property
    def has_api_keys(self) -> bool:
        return bool(self.keys)

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
        Respects daily limits. Raises a clear error when no key, quota, or
        usable response is available so the task does not treat failure as an
        empty result.
        """
        if not self.keys:
            raise NetlasMissingApiKey(
                "No Netlas API key is configured (NETLAS_API_KEY_1 / _2)"
            )

        try:
            r = self._redis()
            account = self._pick_account(r)
        except Exception as e:
            raise NetlasProviderError(
                f"Could not read Netlas account usage: {type(e).__name__}: {e}"
            ) from e

        if account is None:
            raise NetlasDailyLimitReached(
                "Daily request limit reached for all configured Netlas accounts"
            )

        account_num, api_key = account

        # Run in a thread — the Netlas SDK is synchronous. Disable its hidden
        # retry so the task honors Retry-After before making another request.
        def _do_search():
            import netlas
            from netlas.exception import APIError, ThrottlingError

            conn = netlas.Netlas(api_key=api_key)
            try:
                return conn.query(
                    query=query,
                    datatype="response",
                    page=0,
                    throttling=False,
                    retry=0,
                )
            except ThrottlingError as e:
                raise NetlasRateLimitError(e.retry_after) from e
            except APIError as e:
                details = [
                    str(value).strip()
                    for value in (
                        getattr(e, "type", ""),
                        getattr(e, "title", ""),
                        getattr(e, "detail", ""),
                        getattr(e, "value", ""),
                    )
                    if value
                ]
                message = ": ".join(dict.fromkeys(details)) or repr(e)
                lowered = message.lower()
                rejected_key_words = (
                    "unauthorized",
                    "authentication",
                    "access denied",
                    "invalid api key",
                    "api key is invalid",
                    "api_key_invalid",
                    "invalid credential",
                    "invalid token",
                    "forbidden",
                    "401",
                    "403",
                )
                if any(word in lowered for word in rejected_key_words):
                    raise NetlasRejectedApiKey(message) from e
                raise NetlasProviderError(f"Netlas API error: {message}") from e

        try:
            # Count every request attempt so failed calls cannot let later runs
            # pass the daily allowance by repeating requests that did not work.
            self._increment_usage(r, account_num)
            data = await asyncio.to_thread(_do_search)

            used_now = self._get_usage(r, account_num)
            limit = self.DAILY_LIMITS[account_num]
            logger.info(
                f"    [Netlas] Acct#{account_num} used {used_now}/{limit} today | "
                f"query: {query[:60]}..."
            )

            results: list[dict[str, Any]] = []
            items = (data or {}).get("items", [])
            for item in items[: self.RESULTS_PER_REQUEST]:
                d = item.get("data", {})
                http = d.get("http", {})
                body = http.get("body", "") or ""
                ip = d.get("ip", "")
                port = d.get("port", "")
                protocol = d.get("protocol", "http")

                if not isinstance(body, str) or not body:
                    logger.debug("[Netlas] Skipping empty or non-text response body for %s", ip)
                    continue

                for hit in sweep_text_for_tokens(body, "netlas"):
                    results.append({
                        "token": hit["token"],
                        "chat_id": hit["chat_id"],
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

        except NetlasProviderError:
            raise
        except Exception as e:
            raise NetlasProviderError(
                f"Netlas request failed: {type(e).__name__}: {e}"
            ) from e

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
