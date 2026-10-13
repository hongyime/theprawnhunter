"""Scanner client for Shodan."""

from email.utils import parsedate_to_datetime
import time

from ..common import *


class ShodanSearchError(RuntimeError):
    """A search failed; ``stop_scan`` marks provider-wide limits."""

    def __init__(self, message: str, *, stop_scan: bool = False):
        super().__init__(message)
        self.stop_scan = stop_scan


MAX_TRANSIENT_RETRIES = 3
MAX_RATE_LIMIT_RETRIES = 1
MAX_RATE_LIMIT_WAIT_SECONDS = 300
SHODAN_PAGE_SIZE = 100


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])[:300]
    return response.reason_phrase or f"HTTP {response.status_code}"


def _is_credit_or_plan_limit(message: str) -> bool:
    text = message.lower()
    return any(
        phrase in text
        for phrase in (
            "query credit",
            "credits exhausted",
            "credit limit",
            "plan limit",
            "not allowed by your plan",
            "not available for your plan",
            "not included in your plan",
            "requires a paid plan",
            "upgrade your plan",
            "subscription required",
        )
    )


def _retry_after_seconds(response: httpx.Response) -> int:
    value = response.headers.get("Retry-After", "").strip()
    if value.isdigit():
        return max(1, int(value))
    if value:
        try:
            return max(1, int(parsedate_to_datetime(value).timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            pass
    return 60


def _safe_network_message(error: Exception) -> str:
    message = str(error)
    request = getattr(error, "request", None)
    if request is not None:
        # HTTPX error text can include the full request URL, including the API key.
        message = message.replace(str(request.url), "Shodan API request")
    return f"{type(error).__name__}: {message or 'request failed'}"


class ShodanService:
    def __init__(self):
        self.api_key = settings.SHODAN_KEY
        self.base_url = "https://api.shodan.io/shodan/host/search"

    async def search(self, query: str, country_code: str = None) -> list[dict[str, Any]]:
        api_key = (self.api_key or "").strip()
        if not api_key:
            logger.warning(
                "[Shodan] Search %r was not sent because the API key is missing or empty; "
                "it will be tried after SHODAN_KEY is set",
                query,
            )
            raise ShodanSearchError("Shodan API key is missing or empty", stop_scan=True)

        full_query = query
        if country_code:
            full_query = f'{query} country:"{country_code}"'
            logger.info("[Shodan] Adding country filter: %s", country_code)

        params = {"key": api_key, "query": full_query, "page": 1}
        transient_retries = 0
        rate_limit_retries = 0

        while True:
            try:
                async with get_async_http_client(timeout=30.0, follow_redirects=False) as client:
                    response = await client.get(self.base_url, params=params)

                error_message = _response_error(response)
                if response.status_code == 429:
                    if _is_credit_or_plan_limit(error_message):
                        raise ShodanSearchError(
                            f"HTTP 429: {error_message}; stopping this run because the account limit was reached",
                            stop_scan=True,
                        )
                    if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES:
                        raise ShodanSearchError(
                            f"HTTP 429: {error_message}; stopping this run to avoid more requests",
                            stop_scan=True,
                        )

                    wait_seconds = _retry_after_seconds(response)
                    if wait_seconds > MAX_RATE_LIMIT_WAIT_SECONDS:
                        raise ShodanSearchError(
                            f"HTTP 429: Shodan asked for a {wait_seconds}s wait; stopping this run",
                            stop_scan=True,
                        )

                    logger.warning(
                        "[Shodan] Search %r was rate limited; waiting %ss before one retry",
                        full_query,
                        wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
                    rate_limit_retries += 1
                    continue

                if response.status_code >= 500:
                    if transient_retries < MAX_TRANSIENT_RETRIES:
                        wait_seconds = 2 ** (transient_retries + 1)
                        logger.warning(
                            "[Shodan] Search %r got HTTP %s (%s); waiting %ss before retry %s/%s",
                            full_query,
                            response.status_code,
                            error_message,
                            wait_seconds,
                            transient_retries + 1,
                            MAX_TRANSIENT_RETRIES,
                        )
                        await asyncio.sleep(wait_seconds)
                        transient_retries += 1
                        continue
                    raise ShodanSearchError(
                        f"HTTP {response.status_code} after {MAX_TRANSIENT_RETRIES + 1} requests: {error_message}"
                    )

                if response.status_code >= 300:
                    stop_scan = response.status_code == 402 or _is_credit_or_plan_limit(error_message)
                    raise ShodanSearchError(
                        f"HTTP {response.status_code}: {error_message}",
                        stop_scan=stop_scan,
                    )

                try:
                    payload = response.json()
                except Exception as error:
                    raise ShodanSearchError(
                        f"Shodan returned an unreadable reply ({type(error).__name__})"
                    ) from error

                if not isinstance(payload, dict):
                    raise ShodanSearchError("Shodan returned a reply that was not an object")

                if payload.get("error"):
                    error_message = str(payload["error"])[:300]
                    rate_limited = any(
                        phrase in error_message.lower()
                        for phrase in ("rate limit", "too fast", "too many requests")
                    )
                    stop_scan = rate_limited or _is_credit_or_plan_limit(error_message)
                    raise ShodanSearchError(
                        f"Shodan error reply: {error_message}",
                        stop_scan=stop_scan,
                    )

                matches = payload.get("matches")
                if not isinstance(matches, list):
                    raise ShodanSearchError("Shodan reply did not contain a matches list")

                if len(matches) > SHODAN_PAGE_SIZE:
                    logger.warning(
                        "[Shodan] Search %r returned more than one page; keeping the first %s matches",
                        full_query,
                        SHODAN_PAGE_SIZE,
                    )
                    matches = matches[:SHODAN_PAGE_SIZE]
                break

            except httpx.RequestError as error:
                if transient_retries < MAX_TRANSIENT_RETRIES:
                    wait_seconds = 2 ** (transient_retries + 1)
                    logger.warning(
                        "[Shodan] Search %r could not reach Shodan (%s); waiting %ss before retry %s/%s",
                        full_query,
                        _safe_network_message(error),
                        wait_seconds,
                        transient_retries + 1,
                        MAX_TRANSIENT_RETRIES,
                    )
                    await asyncio.sleep(wait_seconds)
                    transient_retries += 1
                    continue
                raise ShodanSearchError(
                    f"Shodan could not be reached after {MAX_TRANSIENT_RETRIES + 1} requests: "
                    f"{_safe_network_message(error)}"
                ) from error

        # The first page is limited to 100 results by Shodan. Avoid extra pages,
        # which can use more query credits.

        # Sort and filter the one page we asked Shodan for.
        from datetime import datetime, timedelta

        three_hours_ago = datetime.utcnow() - timedelta(hours=3)
        matches = sorted(matches, key=lambda item: item.get("timestamp", ""), reverse=True)
        recent_matches = []
        for match in matches:
            try:
                timestamp = match.get("timestamp", "")
                if timestamp:
                    match_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00").split("+")[0])
                    if match_time >= three_hours_ago:
                        recent_matches.append(match)
            except Exception as error:
                logger.warning(
                    "[Shodan] Search %r has a result with an unreadable timestamp (%s); "
                    "keeping the first-page results",
                    full_query,
                    type(error).__name__,
                )

        matches = recent_matches if len(recent_matches) > len(matches[:SHODAN_PAGE_SIZE]) else matches[:SHODAN_PAGE_SIZE]
        results = []
        logger.info("[Shodan] Search %r is processing %s matches", full_query, len(matches))

        async with httpx.AsyncClient(verify=False, timeout=10.0) as scan_client:
            tasks = []
            sem = asyncio.Semaphore(20)

            def report_probe_error(target_url: str, error: Exception):
                if isinstance(error, httpx.HTTPStatusError):
                    detail = f"HTTP {error.response.status_code}"
                elif isinstance(error, RuntimeError):
                    detail = str(error)
                else:
                    detail = type(error).__name__
                logger.warning(
                    "[Shodan] Search %r could not check %s (%s); continuing with other matches",
                    full_query,
                    target_url,
                    detail,
                )

            async def process_match(match):
                async with sem:
                    ip = match.get("ip_str")
                    port = match.get("port")
                    banner = match.get("data", "")

                    tokens_found = TOKEN_PATTERN.findall(banner)
                    chat_ids_found = CHAT_ID_PATTERN.findall(banner)
                    passive_cid = chat_ids_found[0] if chat_ids_found else None
                    local_found = [
                        {"token": token, "chat_id": passive_cid}
                        for token in set(tokens_found)
                        if _is_valid_token(token)
                    ]

                    if not local_found:
                        try:
                            proto = "https" if port == 443 else "http"
                            target_url = f"{proto}://{ip}:{port}"
                            active_found = await _perform_active_deep_scan(
                                target_url,
                                client=scan_client,
                                on_error=report_probe_error,
                            )
                            local_found.extend(active_found)
                        except Exception as error:
                            logger.warning(
                                "[Shodan] Search %r failed while checking %s:%s (%s); "
                                "continuing with other matches",
                                full_query,
                                ip,
                                port,
                                type(error).__name__,
                            )

                    return ip, port, local_found

            tasks = [process_match(match) for match in matches]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

        for match, result in zip(matches, batch_results):
            if isinstance(result, Exception):
                logger.warning(
                    "[Shodan] Search %r could not process host %s:%s (%s); continuing with other matches",
                    full_query,
                    match.get("ip_str"),
                    match.get("port"),
                    type(result).__name__,
                )
                continue

            if not isinstance(result, tuple):
                logger.warning(
                    "[Shodan] Search %r returned an unexpected host result; skipping it and continuing",
                    full_query,
                )
                continue

            ip, port, found_items = result
            seen_tokens = set()
            for item in found_items:
                token = item["token"]
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                results.append(
                    {
                        "token": token,
                        "chat_id": item["chat_id"],
                        "meta": {
                            "ip": ip,
                            "port": port,
                            "shodan_data": "verified_active" if ip else "banner_match",
                        },
                    }
                )

        return results
