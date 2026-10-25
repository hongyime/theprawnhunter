"""Scanner client for Wayback."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.domain.tokens import sweep_text_for_tokens

from ..common import *


@dataclass
class WaybackScanBatch:
    matches: list[dict[str, Any]] = field(default_factory=list)
    completed_snapshot_ids: list[str] = field(default_factory=list)
    snapshot_urls: dict[str, str] = field(default_factory=dict)
    failures: int = 0


class WaybackService:
    """Internet Archive search. It needs no login key."""

    CDX_URL = "https://web.archive.org/cdx/search/cdx"
    ARCHIVE_URL_TEMPLATE = "https://web.archive.org/web/{timestamp}/{url}"
    PENDING_KEY = "wayback:pending"
    MAX_RESULTS = 500
    MAX_REQUESTS = 500
    MAX_RETRIES = 3
    REQUEST_PAUSE = 1.2
    MAX_WAIT = 60.0
    SEEN_TTL = 7 * 86400
    # Bounded CDX query templates. `{username}` is resolved by the pivot layer;
    # the scheduled scan uses the wildcard form. Keep this list short — every
    # extra entry costs one CDX request per run.
    DEFAULT_QUERY_TEMPLATES = (
        "api.telegram.org",
        "t.me/{username}",
        "*/raw/*",
        "*config*.json",
        "*env*",
    )

    @classmethod
    def default_queries(cls, username: str | None = None) -> list[str]:
        """Return the default query set with the pivot placeholder resolved."""
        account = username or "*"
        return [template.format(username=account) for template in cls.DEFAULT_QUERY_TEMPLATES]

    def __init__(self):
        self.timeout = httpx.Timeout(20.0, connect=10.0)

    @staticmethod
    def _url_hash(original: str) -> str:
        return hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:16]

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _reply_problem(body: str) -> str | None:
        text = (body or "").strip().lower()
        if not text:
            return "empty snapshot"
        if any(s in text for s in (
            "too many requests", "rate limit", "rate-limit", "please slow down",
            "temporarily blocked", "access denied", "request blocked", "you have been blocked",
        )):
            return "rate limit or block reply in HTTP 200"
        if any(s in text for s in (
            "not available on the wayback machine", "no captures found",
            "snapshot not found", "this page is not available",
        )):
            return "missing snapshot reply in HTTP 200"
        if any(s in text for s in (
            "wayback machine has encountered an error", "wayback machine error",
            "archive replay error", "internal server error", "something went wrong",
        )):
            return "Wayback error reply in HTTP 200"
        if any(s in text for s in ("still loading", "loading this snapshot", "loading archived page")):
            return "snapshot is still loading"
        return None

    def _load_pending(self, redis_client: Any, batch: WaybackScanBatch) -> dict[str, dict[str, str]]:
        try:
            saved = redis_client.hgetall(self.PENDING_KEY)
        except Exception as error:
            batch.failures += 1
            logger.error("[Wayback] Could not read saved snapshots (%s); will use this search and retry next scan.", error)
            return {}

        pending = {}
        for raw_id, raw_value in saved.items():
            snapshot_id = self._text(raw_id)
            try:
                item = json.loads(self._text(raw_value))
                original, timestamp = item["original"], item["timestamp"]
                if not isinstance(original, str) or not original or not isinstance(timestamp, str) or not timestamp:
                    raise ValueError("missing URL or timestamp")
                pending[snapshot_id] = {"original": original, "timestamp": timestamp}
            except Exception as error:
                batch.failures += 1
                logger.error("[Wayback] Saved snapshot %s is unreadable (%s); it stays saved for next scan.", snapshot_id, error)
        return pending

    def _save_pending(
        self, redis_client: Any, snapshot_id: str, original: str, timestamp: str, query: str,
        batch: WaybackScanBatch,
    ) -> bool:
        try:
            redis_client.hset(self.PENDING_KEY, snapshot_id, json.dumps({"original": original, "timestamp": timestamp}))
            return True
        except Exception as error:
            batch.failures += 1
            logger.error(
                "[Wayback] Could not save snapshot %s (%s) from search %r (%s); it was not fetched and the search repeats next scan.",
                snapshot_id, original, query, error,
            )
            return False

    async def _search_cdx(
        self, client: httpx.AsyncClient, query: str, limit: int, batch: WaybackScanBatch,
    ) -> tuple[list[Any] | None, bool]:
        params = {
            "url": query, "matchType": "domain", "output": "json", "limit": limit,
            "filter": ["statuscode:200", "urlkey:.*bot.*"],
        }
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = await client.get(self.CDX_URL, params=params)
            except Exception as error:
                if attempt < self.MAX_RETRIES:
                    wait = self.REQUEST_PAUSE * (2 ** (attempt - 1))
                    logger.warning("[Wayback] Search %r failed (%s); waiting %.1fs before try %d/%d.", query, error, wait, attempt + 1, self.MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                batch.failures += 1
                logger.error("[Wayback] Search %r failed after %d tries (%s); it will repeat next scan.", query, self.MAX_RETRIES, error)
                return None, False

            if response.status_code in (403, 429):
                wait = self._retry_after(response.headers.get("Retry-After"))
                wait = wait if wait is not None else (10.0 if response.status_code == 403 else self.REQUEST_PAUSE)
                if wait > self.MAX_WAIT:
                    batch.failures += 1
                    logger.error("[Wayback] Search %r got HTTP %d with a %.1fs retry wait; stopping requests this run and retrying next scan.", query, response.status_code, wait)
                    return None, True
                if attempt == 1:
                    logger.warning("[Wayback] Search %r got HTTP %d; waiting %.1fs, then trying once more.", query, response.status_code, max(wait, self.REQUEST_PAUSE))
                    await asyncio.sleep(max(wait, self.REQUEST_PAUSE))
                    continue
                batch.failures += 1
                logger.error("[Wayback] Search %r is blocked or rate limited (HTTP %d); stopping Wayback requests this run, retry next scan.", query, response.status_code)
                return None, True

            if response.status_code >= 500 and attempt < self.MAX_RETRIES:
                wait = self.REQUEST_PAUSE * (2 ** (attempt - 1))
                logger.warning("[Wayback] Search %r returned HTTP %d; waiting %.1fs before try %d/%d.", query, response.status_code, wait, attempt + 1, self.MAX_RETRIES)
                await asyncio.sleep(wait)
                continue
            if response.status_code != 200:
                batch.failures += 1
                logger.error("[Wayback] Search %r returned HTTP %d; it will repeat next scan.", query, response.status_code)
                return None, False
            try:
                rows = response.json()
            except Exception as error:
                batch.failures += 1
                logger.error("[Wayback] Search %r returned bad JSON (%s); it will repeat next scan.", query, error)
                return None, False
            if not isinstance(rows, list):
                batch.failures += 1
                logger.error("[Wayback] Search %r returned an unexpected reply (%s); it will repeat next scan.", query, type(rows).__name__)
                return None, False
            return rows, False
        return None, False

    async def _fetch_snapshot(
        self, client: httpx.AsyncClient, query: str, snapshot: dict[str, str],
        request_budget: int,
    ) -> tuple[str | None, int, bool, bool]:
        original, timestamp = snapshot["original"], snapshot["timestamp"]
        url = self.ARCHIVE_URL_TEMPLATE.format(timestamp=timestamp, url=original)
        requests = failures = rate_retries = 0
        next_wait = self.REQUEST_PAUSE

        while requests < request_budget:
            await asyncio.sleep(min(max(self.REQUEST_PAUSE, next_wait), self.MAX_WAIT))
            next_wait = self.REQUEST_PAUSE
            requests += 1
            try:
                response = await client.get(url)
            except Exception as error:
                failures += 1
                if failures < self.MAX_RETRIES and requests < request_budget:
                    next_wait = self.REQUEST_PAUSE * failures
                    logger.warning("[Wayback] Snapshot %s (%s, search %r) failed (%s); wait %.1fs and retry.", timestamp, original, query, error, next_wait)
                    continue
                logger.error("[Wayback] Snapshot %s (%s, search %r) failed (%s); keep it for next scan.", timestamp, original, query, error)
                return None, requests, False, True

            if response.status_code in (403, 429):
                rate_retries += 1
                wait = self._retry_after(response.headers.get("Retry-After"))
                wait = wait if wait is not None else (10.0 if response.status_code == 403 else self.REQUEST_PAUSE)
                if wait > self.MAX_WAIT:
                    logger.error("[Wayback] Snapshot %s (%s, search %r) got HTTP %d with a %.1fs retry wait; stop and retry next scan.", timestamp, original, query, response.status_code, wait)
                    return None, requests, True, True
                if rate_retries == 1 and requests < request_budget:
                    next_wait = max(wait, self.REQUEST_PAUSE)
                    logger.warning("[Wayback] Snapshot %s (%s, search %r) got HTTP %d; wait %.1fs and retry once.", timestamp, original, query, response.status_code, next_wait)
                    continue
                logger.error("[Wayback] Snapshot %s (%s, search %r) remains blocked or rate limited (HTTP %d); stop fetches this run and retry next scan.", timestamp, original, query, response.status_code)
                return None, requests, True, True

            if response.status_code >= 500 and failures < self.MAX_RETRIES - 1 and requests < request_budget:
                failures += 1
                next_wait = self.REQUEST_PAUSE * failures
                logger.warning("[Wayback] Snapshot %s (%s, search %r) returned HTTP %d; wait %.1fs and retry.", timestamp, original, query, response.status_code, next_wait)
                continue
            if response.status_code != 200:
                logger.error("[Wayback] Snapshot %s (%s, search %r) returned HTTP %d; keep it for next scan and continue.", timestamp, original, query, response.status_code)
                return None, requests, False, True

            body = response.text
            problem = self._reply_problem(body)
            if problem == "rate limit or block reply in HTTP 200":
                rate_retries += 1
                if rate_retries == 1 and requests < request_budget:
                    next_wait = self.REQUEST_PAUSE
                    logger.warning("[Wayback] Snapshot %s (%s, search %r) says %s; wait and retry once.", timestamp, original, query, problem)
                    continue
                logger.error("[Wayback] Snapshot %s (%s, search %r) still says %s; stop fetches and retry next scan.", timestamp, original, query, problem)
                return None, requests, True, True
            if problem:
                logger.warning("[Wayback] Snapshot %s (%s, search %r) returned %s; keep it for next scan and continue.", timestamp, original, query, problem)
                return None, requests, False, True
            return body, requests, False, False

        logger.warning("[Wayback] Snapshot %s (%s, search %r) reached the %d-request cap; keep it for next scan.", timestamp, original, query, request_budget)
        return None, requests, True, True

    async def search(
        self, query_pattern: str | None = None, limit: int = 500, *, return_batch: bool = False,
    ) -> list[dict[str, Any]] | WaybackScanBatch:
        """Search CDX and fetch snapshots; the scanner can request completion details.

        Without an explicit query the bounded ``DEFAULT_QUERY_TEMPLATES`` set is
        used. Every query shares one per-run snapshot request budget so extra
        templates cannot multiply outbound requests.
        """
        from app.workers.tasks.flow_tasks import redis_client

        batch = WaybackScanBatch()
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError):
            requested_limit = self.MAX_RESULTS
            batch.failures += 1
            logger.error("[Wayback] Search %r got invalid result limit %r; using %d.", query_pattern, limit, self.MAX_RESULTS)
        safe_limit = max(1, min(requested_limit, self.MAX_RESULTS))
        if safe_limit != requested_limit:
            logger.warning("[Wayback] Search %r limit %d is outside 1-%d; using %d.", query_pattern, requested_limit, self.MAX_RESULTS, safe_limit)

        queries = [query_pattern] if query_pattern else self.default_queries()
        per_query_items = max(1, safe_limit // len(queries))

        pending = self._load_pending(redis_client, batch)
        for snapshot_id, snapshot in list(pending.items()):
            try:
                already_seen = redis_client.exists(f"wayback:seen:{snapshot_id}")
            except Exception as error:
                batch.failures += 1
                logger.warning("[Wayback] Could not check saved snapshot %s (%s) (%s); keep and retry it.", snapshot_id, snapshot["original"], error)
            else:
                if already_seen:
                    pending.pop(snapshot_id)
                    try:
                        redis_client.hdel(self.PENDING_KEY, snapshot_id)
                    except Exception as error:
                        batch.failures += 1
                        logger.warning("[Wayback] Could not clear completed snapshot %s (%s) from pending (%s); its read marker is kept.", snapshot_id, snapshot["original"], error)
        candidates = dict(pending)
        attempted: set[str] = set()

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            request_budget = min(safe_limit, self.MAX_REQUESTS)
            requests_used = 0
            fetch_stopped = False
            for query in queries:
                if requests_used >= request_budget:
                    batch.failures += 1
                    logger.warning("[Wayback] Search %r reached the %d-request cap; remaining searches stay for next scan.", query, request_budget)
                    break

                rows, stop = await self._search_cdx(client, query, safe_limit, batch)
                if rows is None:
                    logger.warning("[Wayback] Search %r has no new rows this run; saved snapshots remain available.", query)
                elif len(rows) < 2:
                    logger.info("[Wayback] Search %r returned no snapshots.", query)
                else:
                    header = rows[0]
                    try:
                        ts_index, url_index = header.index("timestamp"), header.index("original")
                    except (AttributeError, ValueError):
                        batch.failures += 1
                        logger.error("[Wayback] Search %r returned a bad CDX header %r; try the search next scan.", query, header)
                    else:
                        for row_number, row in enumerate(rows[1:safe_limit + 1], 1):
                            if not isinstance(row, list) or len(row) <= max(ts_index, url_index):
                                batch.failures += 1
                                logger.warning("[Wayback] Search %r returned short row %d (%r); skip it and retry the search next scan.", query, row_number, row)
                                continue
                            timestamp, original = row[ts_index], row[url_index]
                            if not isinstance(timestamp, str) or not timestamp or not isinstance(original, str) or not original:
                                batch.failures += 1
                                logger.warning("[Wayback] Search %r returned empty row %d (%r); skip it and retry next scan.", query, row_number, row)
                                continue
                            snapshot_id = self._url_hash(original)
                            if snapshot_id in candidates:
                                continue
                            try:
                                if redis_client.exists(f"wayback:seen:{snapshot_id}"):
                                    continue
                            except Exception as error:
                                batch.failures += 1
                                logger.warning("[Wayback] Could not check snapshot %s (%s) read marker (%s); save and retry it.", snapshot_id, original, error)
                            if self._save_pending(redis_client, snapshot_id, original, timestamp, query, batch):
                                candidates[snapshot_id] = {"original": original, "timestamp": timestamp}

                if stop:
                    logger.warning("[Wayback] Search %r was blocked; stop requests this run and retry next scan.", query)
                    return batch if return_batch else batch.matches

                items = [
                    (snapshot_id, snapshot)
                    for snapshot_id, snapshot in candidates.items()
                    if snapshot_id not in attempted
                ][:per_query_items]
                for index, (snapshot_id, snapshot) in enumerate(items):
                    if requests_used >= request_budget:
                        batch.failures += 1
                        logger.warning("[Wayback] Search %r reached the %d-request cap; %d snapshots stay saved for next scan.", query, request_budget, len(items) - index)
                        fetch_stopped = True
                        break
                    attempted.add(snapshot_id)
                    body, used, stop, failed = await self._fetch_snapshot(
                        client, query, snapshot, request_budget - requests_used,
                    )
                    requests_used += used
                    batch.failures += int(failed)
                    if stop:
                        if len(items) > index + 1:
                            logger.warning("[Wayback] Wayback stopped this run with %d snapshots saved; they will be tried next scan.", len(items) - index - 1)
                        fetch_stopped = True
                        break
                    if body is None:
                        continue
                    original, timestamp = snapshot["original"], snapshot["timestamp"]
                    try:
                        url_hits = sweep_text_for_tokens(original, "wayback")
                        url_tokens = {hit["token"] for hit in url_hits}
                        body_hits = sweep_text_for_tokens(body, "wayback")
                        for hit in url_hits + [hit for hit in body_hits if hit["token"] not in url_tokens]:
                            batch.matches.append({
                                "token": hit["token"],
                                "chat_id": hit["chat_id"],
                                "context": hit["context"],
                                "meta": {
                                    "wayback_url": original,
                                    "wayback_timestamp": timestamp,
                                    "extracted_from": "url" if hit["token"] in url_tokens else "body",
                                    "source": hit["source"],
                                },
                            })
                        batch.completed_snapshot_ids.append(snapshot_id)
                        batch.snapshot_urls[snapshot_id] = original
                    except Exception as error:
                        batch.failures += 1
                        logger.exception("[Wayback] Snapshot %s (%s, search %r) could not be scanned (%s); keep it for next scan and continue.", timestamp, original, query, error)

                if fetch_stopped:
                    break

        query_label = query_pattern or "default query set"
        if len(candidates) > safe_limit:
            batch.failures += 1
            logger.warning("[Wayback] Search %r has %d extra saved snapshots; they stay for next scan.", query_label, len(candidates) - safe_limit)
        logger.info("[Wayback] Search %r found %d matches; %d snapshots are ready; %d issues remain.", query_label, len(batch.matches), len(batch.completed_snapshot_ids), batch.failures)
        if return_batch:
            return batch
        self.mark_completed(batch.completed_snapshot_ids, batch.snapshot_urls)
        return batch.matches

    def mark_completed(self, snapshot_ids: list[str], snapshot_urls: dict[str, str] | None = None) -> None:
        from app.workers.tasks.flow_tasks import redis_client
        for snapshot_id in snapshot_ids:
            try:
                redis_client.setex(f"wayback:seen:{snapshot_id}", self.SEEN_TTL, "1")
                redis_client.hdel(self.PENDING_KEY, snapshot_id)
            except Exception as error:
                snapshot_url = (snapshot_urls or {}).get(snapshot_id, snapshot_id)
                logger.error("[Wayback] Could not mark snapshot %s (%s) read (%s); it stays saved for next scan.", snapshot_id, snapshot_url, error)
