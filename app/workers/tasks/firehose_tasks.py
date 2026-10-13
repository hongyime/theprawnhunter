"""
GitHub Events firehose — real-time leak detection from public push events.

Architecture:
    api.github.com/events  →  firehose.poll_github_events (every 30s)
        for each PushEvent:
            for each commit:
                fetch commit diff → regex extract tokens → enqueue

Why this beats search_code:
    - Code search has 6+ minute indexing delay AND a 10 req/min rate limit.
    - The events firehose is real-time; we see leaks within 30 seconds of push.
    - Firehose requests stop at 4500 per rolling hour to leave room under the 5000 request budget.

State:
    - Redis ETag (firehose:gh_events:etag, 1h TTL) — short-circuits 304 responses.
    - Redis last_seen_id (firehose:gh_events:last_id, 24h TTL) — pagination cursor.
    - Redis seen_commit_sha (firehose:gh_seen_commit:<sha>, 7d TTL) — dedup.
"""
import asyncio
import hashlib
import logging
import math
import os
import time
import uuid

import httpx

from app.workers.celery_app import _run_sync, app
from app.workers.tasks.flow_tasks import redis_client
from app.workers.tasks.scanner_tasks import (
    _save_credentials_async,
    _send_log_async,
)

logger = logging.getLogger(__name__)

EVENTS_URL = "https://api.github.com/events"
ETAG_KEY = "firehose:gh_events:etag"
LAST_ID_KEY = "firehose:gh_events:last_id"
SEEN_COMMIT_PREFIX = "firehose:gh_seen_commit:"
MAX_PAGES = min(3, max(1, int(os.getenv("FIREHOSE_MAX_PAGES", 3))))  # 100 events × 3 = 300/poll
POLL_TIMEOUT = int(os.getenv("FIREHOSE_POLL_TIMEOUT", 25))
HOURLY_REQUEST_BUDGET = min(4500, max(1, int(os.getenv("FIREHOSE_HOURLY_REQUEST_BUDGET", 4500))))
REQUESTS_KEY = "firehose:gh_events:requests"
RATE_LIMIT_KEY = "firehose:gh_events:pause_until"
PENDING_VALIDATION_PREFIX = "firehose:gh_events:pending_validation:"
RATE_LIMIT_FALLBACK_SECONDS = 60
REQUEST_WINDOW_SECONDS = 3600
_LOCAL_PAUSE_UNTIL = 0.0

_CLAIM_REQUEST_SLOT_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local window_ms = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window_ms)
local count = tonumber(redis.call('ZCARD', KEYS[1]))
if count >= tonumber(ARGV[2]) then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    local wait = 1
    if oldest[2] then
        wait = math.max(1, math.ceil((tonumber(oldest[2]) + window_ms - now) / 1000))
    end
    return {0, wait}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('EXPIRE', KEYS[1], math.ceil(window_ms / 1000) + 1)
return {1, 0}
"""


@app.task(
    name="firehose.poll_github_events",
    autoretry_for=(Exception,),
    retry_backoff=False,
    max_retries=0,
    # If a poll takes >29s, drop it — the next one will pick up.
    soft_time_limit=29,
    time_limit=35,
)
def poll_github_events():
    """Poll GitHub Events firehose; fan out PushEvent commits to scanner pipeline."""
    try:
        return _run_sync(_poll_github_events_async())
    except Exception as error:
        logger.exception(
            "[Firehose] unexpected poll error: %s; the task stopped cleanly and the next scheduled poll will retry work not marked complete.",
            error,
        )
        return "poll_error"


def _claim_hourly_request_slot():
    """Reserve one request in the shared rolling-hour firehose budget."""
    try:
        result = redis_client.eval(
            _CLAIM_REQUEST_SLOT_SCRIPT,
            1,
            REQUESTS_KEY,
            REQUEST_WINDOW_SECONDS * 1000,
            HOURLY_REQUEST_BUDGET,
            uuid.uuid4().hex,
        )
        return bool(int(result[0])), int(result[1])
    except Exception as error:
        raise RuntimeError(f"could not check the shared hourly request budget: {error}") from error


def _read_pause_until():
    """Read the shared rate-limit wait, while keeping a local fallback."""
    global _LOCAL_PAUSE_UNTIL
    now = time.time()
    if _LOCAL_PAUSE_UNTIL > now:
        return _LOCAL_PAUSE_UNTIL
    try:
        value = redis_client.get(RATE_LIMIT_KEY)
    except Exception as error:
        raise RuntimeError(f"could not read the shared GitHub wait: {error}") from error
    if isinstance(value, bytes):
        value = value.decode()
    if not value:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"the shared GitHub wait has an invalid value: {value!r}") from error


def _set_pause(wait_seconds: float, reason: str):
    """Pause this worker and, when Redis is available, every firehose worker."""
    global _LOCAL_PAUSE_UNTIL
    wait_seconds = max(1, int(math.ceil(wait_seconds)))
    pause_until = time.time() + wait_seconds
    _LOCAL_PAUSE_UNTIL = max(_LOCAL_PAUSE_UNTIL, pause_until)
    try:
        redis_client.setex(RATE_LIMIT_KEY, wait_seconds, str(pause_until))
    except Exception as error:
        logger.error(
            "[Firehose] %s; could not share the %s-second wait through Redis (%s). This worker will wait, and it will stop before making more GitHub requests.",
            reason,
            wait_seconds,
            error,
        )
    logger.warning(
        "[Firehose] %s; waiting %s seconds before another GitHub request. This poll will stop cleanly and keep the event marker unchanged.",
        reason,
        wait_seconds,
    )


def _response_message(response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])
    return str(getattr(response, "text", "") or "").strip()


def _rate_limit_wait_seconds(response) -> int | None:
    """Return GitHub's requested wait for a primary or secondary rate limit."""
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", {}) or {}
    message = _response_message(response).lower()
    is_limited = status == 429 or (
        status == 403
        and (
            str(headers.get("X-RateLimit-Remaining", "")) == "0"
            or "rate limit" in message
        )
    )
    if not is_limited:
        return None

    wait_seconds = 0.0
    retry_after = headers.get("Retry-After")
    try:
        if retry_after is not None:
            wait_seconds = max(wait_seconds, float(retry_after))
    except (TypeError, ValueError):
        pass
    reset_at = headers.get("X-RateLimit-Reset")
    try:
        if reset_at is not None:
            wait_seconds = max(wait_seconds, float(reset_at) - time.time())
    except (TypeError, ValueError):
        pass
    return max(1, int(math.ceil(wait_seconds))) if wait_seconds > 0 else RATE_LIMIT_FALLBACK_SECONDS


async def _fetch_commit(client, event_id: str, commit: dict):
    """Fetch one commit while keeping failed fetches available for the next poll."""
    commit_url = commit.get("url")
    commit_sha = str(commit.get("sha") or "")
    if not commit_url:
        logger.warning(
            "[Firehose] event %s has a commit without a URL; it will be tried again next poll and the event marker will stay unchanged.",
            event_id,
        )
        return [], None, True, False
    if not commit_sha:
        logger.warning(
            "[Firehose] commit %s in event %s has no SHA; it will be fetched without a seen mark and tried again next poll.",
            commit_url,
            event_id,
        )

    seen_key = f"{SEEN_COMMIT_PREFIX}{commit_sha}" if commit_sha else None
    if seen_key:
        try:
            if redis_client.exists(seen_key):
                return [], None, False, False
        except Exception as error:
            logger.warning(
                "[Firehose] could not check commit %s in Redis (%s); it will be tried again next poll and the event marker will stay unchanged.",
                commit_sha,
                error,
            )
            return [], None, True, False

    try:
        allowed, wait_seconds = _claim_hourly_request_slot()
    except Exception as error:
        logger.error(
            "[Firehose] could not check the shared hourly request budget (%s); no commit request was made and this poll will stop. The next poll will try again.",
            error,
        )
        return [], None, True, True
    if not allowed:
        _set_pause(
            wait_seconds,
            f"the hourly request budget of {HOURLY_REQUEST_BUDGET} requests is full",
        )
        return [], None, True, True

    try:
        response = await client.get(commit_url)
    except Exception as error:
        logger.warning(
            "[Firehose] commit %s fetch failed: %s; it will be tried again next poll and the event marker will stay unchanged.",
            commit_sha or commit_url,
            error,
        )
        await asyncio.sleep(0.2)
        return [], None, True, False
    await asyncio.sleep(0.2)

    if response.status_code != 200:
        message = _response_message(response) or "no error message from GitHub"
        wait_seconds = _rate_limit_wait_seconds(response)
        if wait_seconds is not None:
            _set_pause(
                wait_seconds,
                f"GitHub rate limit fetching commit {commit_sha or commit_url}: HTTP {response.status_code} ({message})",
            )
            return [], None, True, True
        logger.warning(
            "[Firehose] commit %s returned HTTP %s (%s); it will be tried again next poll and the event marker will stay unchanged.",
            commit_sha or commit_url,
            response.status_code,
            message,
        )
        return [], None, True, False

    try:
        commit_data = response.json()
        if not isinstance(commit_data, dict):
            raise ValueError("GitHub returned commit data that was not an object")
        files = commit_data.get("files") or []
        if not isinstance(files, list):
            raise ValueError("GitHub returned a files value that was not a list")
    except Exception as error:
        logger.warning(
            "[Firehose] commit %s reply could not be read: %s; it will be tried again next poll and the event marker will stay unchanged.",
            commit_sha or commit_url,
            error,
        )
        return [], None, True, False

    patches = []
    complete = bool(commit_sha)
    if not commit_sha:
        complete = False
    for file_entry in files:
        if not isinstance(file_entry, dict):
            logger.warning(
                "[Firehose] commit %s has a malformed file entry; the commit will be tried again next poll.",
                commit_sha or commit_url,
            )
            complete = False
            continue
        patch = file_entry.get("patch")
        if patch:
            if isinstance(patch, str):
                patches.append(patch)
            else:
                logger.warning(
                    "[Firehose] commit %s has a non-text patch; the commit will be tried again next poll.",
                    commit_sha or commit_url,
                )
                complete = False

    return patches, seen_key if complete else None, not complete, False


async def _process_event(client, event: dict, token_pattern, is_valid_token):
    """Process one event; return its results, completed commits, failure, and stop signal."""
    event_id = str(event.get("id") or "unknown")
    event_type = event.get("type")
    valid_types = {"PushEvent", "IssuesEvent", "PullRequestEvent", "IssueCommentEvent"}
    if event_type not in valid_types:
        return [], set(), False, False

    try:
        payload = event.get("payload") or {}
        repo_name = (event.get("repo") or {}).get("name")
        source_meta = {
            "repo": repo_name,
            "source_kind": f"github_{event_type.lower()}",
        }
        text_blobs = []
        seen_keys = set()
        failed = False
        stop_poll = False

        if event_type == "PushEvent":
            commits = payload.get("commits") or []
            if not isinstance(commits, list):
                raise ValueError("push event commits were not a list")
            for commit in commits:
                if not isinstance(commit, dict):
                    logger.warning(
                        "[Firehose] event %s has a malformed commit; the event marker will stay unchanged and it will be tried again next poll.",
                        event_id,
                    )
                    failed = True
                    continue
                patches, seen_key, commit_failed, stop = await _fetch_commit(client, event_id, commit)
                text_blobs.extend(patches)
                if seen_key:
                    seen_keys.add(seen_key)
                if commit_failed:
                    failed = True
                if commit.get("sha"):
                    source_meta["commit_sha"] = str(commit["sha"])
                if commit.get("url"):
                    source_meta["commit_url"] = commit["url"]
                if stop:
                    stop_poll = True
                    break

        elif event_type == "IssuesEvent":
            issue = payload.get("issue") or {}
            if issue.get("body"):
                text_blobs.append(issue["body"])
            source_meta["issue_url"] = issue.get("html_url")

        elif event_type == "PullRequestEvent":
            pull_request = payload.get("pull_request") or {}
            if pull_request.get("body"):
                text_blobs.append(pull_request["body"])
            source_meta["pr_url"] = pull_request.get("html_url")

        elif event_type == "IssueCommentEvent":
            comment = payload.get("comment") or {}
            if comment.get("body"):
                text_blobs.append(comment["body"])
            source_meta["comment_url"] = comment.get("html_url")

        event_results = []
        if text_blobs:
            tokens = set(token_pattern.findall("\n".join(text_blobs)))
            for token in tokens:
                if is_valid_token(token):
                    event_results.append({"token": token, "meta": source_meta.copy()})
        return event_results, seen_keys, failed, stop_poll
    except Exception as error:
        logger.warning(
            "[Firehose] event %s could not be processed: %s; it will be tried again next poll and the event marker will stay unchanged.",
            event_id,
            error,
        )
        return [], set(), True, False


async def _save_firehose_results(results):
    """Keep the shared recent-token mark from hiding a failed queue attempt."""
    pending_marks = {}
    for item in results:
        token = item.get("token")
        if not token or token == "MANUAL_REVIEW_REQUIRED":
            continue
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        recent_key = f"validated:recent:{token_hash}"
        pending_key = f"{PENDING_VALIDATION_PREFIX}{token_hash}"
        try:
            if redis_client.exists(pending_key):
                redis_client.delete(recent_key, pending_key)
            if redis_client.get(recent_key):
                continue
            redis_client.setex(pending_key, 86400, "1")
            pending_marks[recent_key] = pending_key
        except Exception as error:
            logger.error(
                "[Firehose] could not prepare retry state for a result (%s); no result queue was attempted and the event marker will stay unchanged for the next poll.",
                error,
            )
            return 0, False

    try:
        found_total = await _save_credentials_async(results, "firehose_gh_events")
    except Exception as error:
        for recent_key in pending_marks:
            try:
                redis_client.delete(recent_key)
            except Exception as cleanup_error:
                logger.error(
                    "[Firehose] could not clear a recent-result mark after queue failure (%s); its pending retry mark remains for the next poll (%s).",
                    cleanup_error,
                    error,
                )
        logger.exception(
            "[Firehose] result queue failed (%s); commit marks and the event marker will stay unchanged so the work can be tried again next poll.",
            error,
        )
        return 0, False

    for pending_key in pending_marks.values():
        try:
            redis_client.delete(pending_key)
        except Exception as error:
            logger.error(
                "[Firehose] could not clear a pending-result mark (%s); commit marks and the event marker will stay unchanged so the result can be checked next poll.",
                error,
            )
            return found_total, False
    return found_total, True


async def _poll_github_events_async():
    try:
        if redis_client.get("system:paused"):
            return "paused"
    except Exception as error:
        logger.error(
            "[Firehose] could not check the system pause flag (%s); no GitHub request was made and the next poll will try again.",
            error,
        )
        return "state_error"

    from app.services.scanners import TOKEN_PATTERN, GithubService, _is_valid_token
    try:
        token = GithubService()._get_token()
    except Exception as error:
        logger.error(
            "[Firehose] could not select a GitHub login token (%s); the next scheduled poll will try again.",
            error,
        )
        return "token_error"
    if not token:
        logger.warning(
            "[Firehose] no GitHub login token is set; this poll stopped and the next scheduled poll will try again."
        )
        return "no_token"

    try:
        pause_until = _read_pause_until()
    except Exception as error:
        logger.error(
            "[Firehose] could not check the shared GitHub wait (%s); no request was made and the next poll will try again.",
            error,
        )
        return "state_error"
    if pause_until > time.time():
        wait_seconds = max(1, int(math.ceil(pause_until - time.time())))
        logger.warning(
            "[Firehose] GitHub requests are paused for another %s seconds; this poll will stop without moving the event marker.",
            wait_seconds,
        )
        return "rate_limited"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        etag = redis_client.get(ETAG_KEY)
        last_seen_id = redis_client.get(LAST_ID_KEY)
    except Exception as error:
        logger.error(
            "[Firehose] could not read poll state from Redis (%s); no GitHub request was made and the next poll will try again.",
            error,
        )
        return "state_error"
    if isinstance(etag, bytes):
        etag = etag.decode()
    if etag:
        headers["If-None-Match"] = etag
    if isinstance(last_seen_id, bytes):
        last_seen_id = last_seen_id.decode()

    new_last_id = None
    new_etag = None
    results_to_enqueue = []
    pending_seen_keys = set()
    poll_failed = False
    stop_poll = False
    found_total = 0

    try:
        async with httpx.AsyncClient(timeout=POLL_TIMEOUT, headers=headers) as client:
            for page in range(1, MAX_PAGES + 1):
                try:
                    allowed, wait_seconds = _claim_hourly_request_slot()
                except Exception as error:
                    logger.error(
                        "[Firehose] could not check the shared hourly request budget (%s); no GitHub request was made and this poll will stop. The next poll will try again.",
                        error,
                    )
                    poll_failed = True
                    break
                if not allowed:
                    _set_pause(
                        wait_seconds,
                        f"the hourly request budget of {HOURLY_REQUEST_BUDGET} requests is full",
                    )
                    poll_failed = True
                    break

                try:
                    response = await client.get(f"{EVENTS_URL}?per_page=100&page={page}")
                except Exception as error:
                    logger.warning(
                        "[Firehose] events page %s fetch failed: %s; the event marker will stay unchanged and this page will be tried again next poll.",
                        page,
                        error,
                    )
                    poll_failed = True
                    break

                if response.status_code == 304:
                    if page == 1:
                        logger.info("[Firehose] no new events; the saved event marker is unchanged")
                        return "ok:no_change"
                    logger.warning(
                        "[Firehose] events page %s returned HTTP 304 after earlier pages; the event marker will stay unchanged and the pages will be tried again next poll.",
                        page,
                    )
                    poll_failed = True
                    break

                if response.status_code != 200:
                    message = _response_message(response) or "no error message from GitHub"
                    wait_seconds = _rate_limit_wait_seconds(response)
                    if wait_seconds is not None:
                        _set_pause(
                            wait_seconds,
                            f"GitHub rate limit on events page {page}: HTTP {response.status_code} ({message})",
                        )
                    else:
                        logger.warning(
                            "[Firehose] events page %s returned HTTP %s (%s); the event marker will stay unchanged and this page will be tried again next poll.",
                            page,
                            response.status_code,
                            message,
                        )
                    poll_failed = True
                    break

                try:
                    events = response.json()
                    if not isinstance(events, list):
                        raise ValueError("GitHub returned an event reply that was not a list")
                except Exception as error:
                    logger.warning(
                        "[Firehose] events page %s JSON failed: %s; the event marker will stay unchanged and this page will be tried again next poll.",
                        page,
                        error,
                    )
                    poll_failed = True
                    break

                if page == 1:
                    new_etag = response.headers.get("ETag")
                if not events:
                    logger.info("[Firehose] GitHub returned no more events; the saved event marker is unchanged")
                    break

                reached_old_marker = False
                for event in events:
                    if not isinstance(event, dict):
                        logger.warning(
                            "[Firehose] page %s contains a malformed event; the event marker will stay unchanged and the event will be tried again next poll.",
                            page,
                        )
                        poll_failed = True
                        continue
                    event_id = event.get("id")
                    if last_seen_id and event_id is not None and str(event_id) == str(last_seen_id):
                        reached_old_marker = True
                        break
                    if not event_id:
                        logger.warning(
                            "[Firehose] page %s contains an event without an ID; the event marker will stay unchanged and it will be tried again next poll.",
                            page,
                        )
                        poll_failed = True
                        continue
                    if new_last_id is None:
                        new_last_id = str(event_id)

                    event_results, event_seen_keys, event_failed, stop = await _process_event(
                        client, event, TOKEN_PATTERN, _is_valid_token
                    )
                    results_to_enqueue.extend(event_results)
                    pending_seen_keys.update(event_seen_keys)
                    if event_failed:
                        poll_failed = True
                    if stop:
                        stop_poll = True
                        break

                if stop_poll or reached_old_marker or len(events) < 100:
                    break
    except Exception as error:
        logger.exception(
            "[Firehose] unexpected provider or poll error: %s; the event marker will stay unchanged and incomplete work will be tried again next poll.",
            error,
        )
        poll_failed = True

    save_succeeded = True
    if results_to_enqueue:
        found_total, save_succeeded = await _save_firehose_results(results_to_enqueue)
        if not save_succeeded:
            poll_failed = True
        if found_total:
            try:
                await _send_log_async(f"⚡ [Firehose] enqueued {found_total} tokens")
            except Exception as error:
                logger.warning(
                    "[Firehose] tokens were queued, but the system log failed: %s; queued work is complete and the poll will continue.",
                    error,
                )

    if save_succeeded:
        for seen_key in pending_seen_keys:
            try:
                redis_client.setex(seen_key, 7 * 86400, "1")
            except Exception as error:
                poll_failed = True
                logger.warning(
                    "[Firehose] commit %s seen mark failed (%s); it will be fetched again next poll and the event marker will stay unchanged.",
                    seen_key.removeprefix(SEEN_COMMIT_PREFIX),
                    error,
                )

    if new_last_id and not poll_failed:
        try:
            redis_client.setex(LAST_ID_KEY, 86400, new_last_id)
        except Exception as error:
            poll_failed = True
            logger.warning(
                "[Firehose] event marker save failed for %s (%s); the next poll will retry the same events.",
                new_last_id,
                error,
            )

    if new_etag and not poll_failed:
        try:
            redis_client.setex(ETAG_KEY, 3600, new_etag)
        except Exception as error:
            logger.warning(
                "[Firehose] ETag save failed (%s); the next poll may fetch pages again, while the saved event marker protects completed work.",
                error,
            )

    if poll_failed:
        logger.warning(
            "[Firehose] poll finished with incomplete work; the event marker was kept unchanged and incomplete work will be tried again next poll."
        )

    logger.info("[Firehose] poll complete, enqueued %s tokens", found_total)
    return f"partial:{found_total}" if poll_failed else f"ok:{found_total}"
