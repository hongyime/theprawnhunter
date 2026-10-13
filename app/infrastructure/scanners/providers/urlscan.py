"""Scanner client for UrlScan."""

from ..common import *


class UrlScanError(RuntimeError):
    """A URLScan search failed and should be tried again later."""


class UrlScanRateLimitError(UrlScanError):
    """URLScan asked this run to stop because of a request limit."""

    rate_limited = True


class UrlScanResults(list):
    """Found tokens and scan lookups that still need work."""

    def __init__(self, values=(), **state):
        super().__init__(values)
        self.pending_scans = state.get("pending_scans", [])
        self.completed_scans = state.get("completed_scans", [])
        self.attempted_scan_ids = state.get("attempted_scan_ids", set())
        self.rate_limited = state.get("rate_limited", False)
        self.retry_query = state.get("retry_query", False)


class UrlScanService:
    """Search URLScan and inspect its saved page data."""

    SEARCH_URL = "https://urlscan.io/api/v1/search/"
    MAX_RESULTS = 100
    MAX_RETRIES = 3
    MAX_INLINE_WAIT = 60

    def __init__(self):
        self.api_key = settings.URLSCAN_KEY

    @staticmethod
    def _retry_after(response) -> int:
        value = response.headers.get("Retry-After", "")
        return int(value) if value.isdigit() else 60

    @staticmethod
    def _body(response) -> str:
        try:
            return response.text.strip().replace("\n", " ")[:240]
        except Exception:
            return ""

    @staticmethod
    def _dom_tokens(content: str) -> list[dict]:
        tokens = TOKEN_PATTERN.findall(content)
        chat_ids = CHAT_ID_PATTERN.findall(content)
        chat_id = chat_ids[0] if chat_ids else None
        return [{"token": token, "chat_id": chat_id} for token in tokens]

    async def _search_request(self, query: str, api_query: str, headers: dict) -> dict:
        delay = 2
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                async with get_async_http_client(
                    timeout=30.0, follow_redirects=False
                ) as client:
                    response = await client.get(
                        self.SEARCH_URL,
                        headers=headers,
                        params={"q": api_query, "size": self.MAX_RESULTS},
                    )
            except (httpx.TimeoutException, httpx.RequestError) as error:
                if attempt == self.MAX_RETRIES:
                    raise UrlScanError(
                        f"search {query!r} could not reach URLScan after "
                        f"{attempt + 1} tries: {error}"
                    ) from error
                logger.warning(
                    "[URLScan] Search %r could not reach the service: %s. "
                    "Waiting %ss before retry %s/%s.",
                    query, error, delay, attempt + 2, self.MAX_RETRIES + 1,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            status = response.status_code
            body = self._body(response)
            limited = status == 429 or (
                status == 403 and any(w in body.lower() for w in ("rate", "limit", "quota"))
            )
            if limited:
                wait = self._retry_after(response)
                if attempt == self.MAX_RETRIES or wait > self.MAX_INLINE_WAIT:
                    raise UrlScanRateLimitError(
                        f"search {query!r} hit HTTP {status}; URLScan asked for a "
                        f"{wait}s wait. This run will stop and retry it next time."
                    )
                logger.warning(
                    "[URLScan] Search %r hit HTTP %s. Waiting %ss before retry %s/%s.",
                    query, status, wait, attempt + 2, self.MAX_RETRIES + 1,
                )
                await asyncio.sleep(wait)
                continue

            if status in (500, 502, 503, 504):
                if attempt == self.MAX_RETRIES:
                    raise UrlScanError(
                        f"search {query!r} failed with HTTP {status} after "
                        f"{attempt + 1} tries: {body or 'no error details'}"
                    )
                logger.warning(
                    "[URLScan] Search %r got HTTP %s: %s. Waiting %ss before retry %s/%s.",
                    query, status, body or "no error details", delay,
                    attempt + 2, self.MAX_RETRIES + 1,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if status in (401, 403):
                raise UrlScanError(
                    f"search {query!r} was refused with HTTP {status}; "
                    f"check the URLScan API key. {body}"
                )
            if status != 200:
                raise UrlScanError(
                    f"search {query!r} failed with HTTP {status}: {body or 'no error details'}"
                )
            try:
                data = response.json()
            except Exception as error:
                raise UrlScanError(f"search {query!r} returned invalid JSON: {error}") from error
            if not isinstance(data, dict):
                raise UrlScanError(f"search {query!r} returned a reply that was not an object")
            if data.get("error") or (
                "results" not in data and (data.get("message") or data.get("status"))
            ):
                detail = data.get("error") or data.get("message") or data.get("status")
                if any(word in str(detail).lower() for word in ("rate", "limit", "quota")):
                    raise UrlScanRateLimitError(
                        f"search {query!r} returned a request limit in its HTTP 200 reply: "
                        f"{str(detail)[:240]}. This run will stop and retry later."
                    )
                raise UrlScanError(
                    f"search {query!r} returned an error in its HTTP 200 reply: "
                    f"{str(detail)[:240]}"
                )
            if not isinstance(data.get("results"), list):
                raise UrlScanError(f"search {query!r} returned no results list in its HTTP 200 reply")
            return data

        raise UrlScanError(f"search {query!r} stopped without a reply")

    async def search(self, query: str, country_code: str = None, *, exclude_scan_ids=None):
        if not self.api_key:
            logger.warning(
                "[URLScan] API key is missing or empty for search %r; "
                "no request was sent. The worker will retry it later.", query,
            )
            return UrlScanResults()

        api_query = f"page.url:*{query}*"
        if country_code:
            api_query = f'({api_query}) AND page.country:"{country_code}"'
        headers = {"API-Key": self.api_key, "Content-Type": "application/json"}
        logger.info("[URLScan] Searching query %r.", query)
        data = await self._search_request(query, api_query, headers)
        items = data["results"][: self.MAX_RESULTS]
        logger.info("[URLScan] Search %r returned %s hits.", query, len(items))
        return await self._process_items(
            query, country_code, items, headers, exclude_scan_ids=exclude_scan_ids or set()
        )

    async def process_pending_scans(self, pending_scans: list[dict]) -> UrlScanResults:
        if not pending_scans:
            return UrlScanResults()
        if not self.api_key:
            logger.warning(
                "[URLScan] API key is missing or empty; keeping %s scan lookups for later.",
                len(pending_scans),
            )
            return UrlScanResults(pending_scans=pending_scans)

        work, deferred = pending_scans[: self.MAX_RESULTS], pending_scans[self.MAX_RESULTS :]
        if deferred:
            logger.info("[URLScan] Leaving %s scan lookups for a later run.", len(deferred))
        headers = {"API-Key": self.api_key, "Content-Type": "application/json"}
        groups = {}
        for record in work:
            if not record.get("query") or not isinstance(record.get("item"), dict):
                logger.error("[URLScan] Saved scan lookup is malformed; leaving it for later: %r", record)
                continue
            key = (record["query"], record.get("country_code"))
            groups.setdefault(key, []).append(record["item"])

        combined = UrlScanResults(pending_scans=deferred)
        grouped = list(groups.items())
        for index, ((query, country), items) in enumerate(grouped):
            result = await self._process_items(query, country, items, headers, force_items=True)
            combined.extend(result)
            combined.pending_scans.extend(result.pending_scans)
            pending_ids = {r.get("item", {}).get("_id") for r in result.pending_scans}
            combined.completed_scans.extend(
                {"query": query, "country_code": country, "item": item}
                for item in items
                if item.get("_id") in result.attempted_scan_ids
                and item.get("_id") not in pending_ids
            )
            combined.attempted_scan_ids.update(result.attempted_scan_ids)
            combined.rate_limited |= result.rate_limited
            combined.retry_query |= result.retry_query
            if result.rate_limited:
                combined.pending_scans.extend(
                    {"query": q, "country_code": c, "item": item}
                    for (q, c), rest in grouped[index + 1 :]
                    for item in rest
                )
                break
        return combined

    async def _dom_lookup(self, client, query, scan_id, headers, lock, rate_event):
        async with lock:
            if rate_event.is_set():
                logger.warning(
                    "[URLScan] Search %r scan %s DOM lookup left for the next run "
                    "after a rate limit.", query, scan_id,
                )
                return [], True
            await asyncio.sleep(1)
            url = f"https://urlscan.io/dom/{scan_id}"
            try:
                response = await client.get(url, headers=headers)
            except Exception as error:
                logger.warning(
                    "[URLScan] Search %r scan %s DOM lookup failed: %s; retrying later.",
                    query, scan_id, error,
                )
                return [], True

            if response.status_code == 404:
                logger.info(
                    "[URLScan] Search %r scan %s DOM is not ready (HTTP 404); retrying later.",
                    query, scan_id,
                )
                return [], True
            if response.status_code in (403, 429):
                body = self._body(response)
                limited = response.status_code == 429 or any(
                    word in body.lower() for word in ("rate", "limit", "quota")
                )
                if not limited:
                    logger.error(
                        "[URLScan] Search %r scan %s DOM was refused (HTTP 403): %s; retrying later.",
                        query, scan_id, body,
                    )
                    return [], True
                wait = self._retry_after(response)
                if wait > self.MAX_INLINE_WAIT:
                    logger.warning(
                        "[URLScan] Search %r scan %s was limited; URLScan asked for %ss. "
                        "Stopping DOM lookups for this run and retrying later.",
                        query, scan_id, wait,
                    )
                    rate_event.set()
                    return [], True
                logger.warning(
                    "[URLScan] Search %r scan %s was limited. Waiting %ss, then trying once more.",
                    query, scan_id, wait,
                )
                await asyncio.sleep(wait)
                try:
                    response = await client.get(url, headers=headers)
                except Exception as error:
                    logger.warning(
                        "[URLScan] Search %r scan %s DOM retry failed: %s; retrying later.",
                        query, scan_id, error,
                    )
                    rate_event.set()
                    return [], True
                if response.status_code == 200:
                    return self._dom_tokens(response.text), False
                logger.warning(
                    "[URLScan] Search %r scan %s stayed limited (HTTP %s); "
                    "stopping DOM lookups for this run.", query, scan_id, response.status_code,
                )
                rate_event.set()
                return [], True
            if response.status_code != 200:
                logger.warning(
                    "[URLScan] Search %r scan %s DOM failed with HTTP %s: %s; retrying later.",
                    query, scan_id, response.status_code, self._body(response),
                )
                return [], True
            return self._dom_tokens(response.text), False

    async def _process_items(
        self, query, country_code, items, headers, *, exclude_scan_ids=None, force_items=False
    ) -> UrlScanResults:
        from datetime import datetime, timedelta

        excluded = exclude_scan_ids or set()
        cutoff = datetime.now(UTC) - timedelta(hours=3)
        recent, unknown, original = [], [], []
        retry_query = False
        for item in items:
            if not isinstance(item, dict):
                logger.error("[URLScan] Search %r returned a malformed scan; retrying the search later.", query)
                retry_query = True
                continue
            scan_id = item.get("_id")
            if scan_id and scan_id in excluded:
                continue
            original.append(item)
            task = item.get("task")
            task = task if isinstance(task, dict) else {}
            timestamp = task.get("time", "")
            if not timestamp:
                unknown.append(item)
                logger.warning("[URLScan] Search %r scan %s has no time; checking it anyway.", query, scan_id or "(no ID)")
                continue
            try:
                scanned_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if scanned_at.tzinfo is None:
                    scanned_at = scanned_at.replace(tzinfo=UTC)
                if scanned_at >= cutoff:
                    recent.append(item)
            except Exception as error:
                unknown.append(item)
                logger.warning("[URLScan] Search %r scan %s has bad time %r: %s; checking it anyway.", query, scan_id or "(no ID)", timestamp, error)

        selected = original if force_items else (recent + unknown) or original[:50]
        selected = selected[: self.MAX_RESULTS]
        if not selected:
            return UrlScanResults(retry_query=retry_query)

        lock = asyncio.Lock()
        rate_event = asyncio.Event()
        semaphore = asyncio.Semaphore(20)
        attempted, pending, found = set(), [], []
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            async def process(item):
                scan_id = item.get("_id")
                tokens, failed = [], False
                if scan_id:
                    attempted.add(scan_id)
                task = item.get("task")
                task = task if isinstance(task, dict) else {}
                status = str(task.get("status") or item.get("status") or "").lower()
                if status in {"queued", "pending", "running", "processing", "in_progress", "in progress", "in-progress"} and not force_items:
                    logger.info("[URLScan] Search %r scan %s is still %s; checking it next run.", query, scan_id or "(no ID)", status)
                    return item, tokens, True

                async with semaphore:
                    try:
                        if scan_id:
                            dom_tokens, dom_failed = await self._dom_lookup(client, query, scan_id, headers, lock, rate_event)
                            tokens.extend(dom_tokens)
                            failed |= dom_failed
                        page = item.get("page")
                        page = page if isinstance(page, dict) else {}
                        page_url = page.get("url", "")
                        if page_url:
                            url_chat = CHAT_ID_PATTERN.findall(page_url)
                            chat_id = url_chat[0] if url_chat else None
                            tokens.extend({"token": token, "chat_id": chat_id} for token in TOKEN_PATTERN.findall(page_url))

                            def report_error(failed_url, error):
                                nonlocal failed
                                logger.warning(
                                    "[URLScan] Search %r scan %s page lookup %s failed: %s; retrying this scan later.",
                                    query, scan_id or "(no ID)", failed_url, error,
                                )
                                failed = True

                            tokens.extend(await _perform_active_deep_scan(
                                page_url, client=client, on_error=report_error
                            ))
                        return item, tokens, failed
                    except Exception as error:
                        logger.error("[URLScan] Search %r scan %s failed: %s; retrying later.", query, scan_id or "(no ID)", error)
                        return item, tokens, True

            outcomes = await asyncio.gather(*(process(item) for item in selected), return_exceptions=True)

        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.error("[URLScan] Search %r scan lookup failed: %s; retrying the search later.", query, outcome)
                retry_query = True
                continue
            item, tokens, failed = outcome
            if failed:
                if item.get("_id"):
                    pending.append({"query": query, "country_code": country_code, "item": item})
                else:
                    retry_query = True
            token_map = {}
            for result in tokens:
                token = result["token"]
                chat_id = result["chat_id"]
                if token not in token_map or (not token_map[token] and chat_id):
                    token_map[token] = chat_id
            for token, chat_id in token_map.items():
                if _is_valid_token(token):
                    found.append({
                        "token": token,
                        "chat_id": chat_id,
                        "meta": {
                            "source": "urlscan",
                            "url": item.get("page", {}).get("url", ""),
                            "domain": (item.get("page") or {}).get("domain")
                            if isinstance(item.get("page") or {}, dict)
                            else None,
                            "scan_id": item.get("_id"),
                            "type": "cached_or_live",
                        },
                    })

        return UrlScanResults(
            found,
            pending_scans=pending,
            attempted_scan_ids=attempted,
            rate_limited=rate_event.is_set(),
            retry_query=retry_query,
        )


__all__ = ["UrlScanError", "UrlScanRateLimitError", "UrlScanResults", "UrlScanService"]
