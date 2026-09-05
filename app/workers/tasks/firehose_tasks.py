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
    - 5000 req/hour authenticated budget is plenty for 30s polls + commit fetches.

State:
    - Redis ETag (firehose:gh_events:etag, 1h TTL) — short-circuits 304 responses.
    - Redis last_seen_id (firehose:gh_events:last_id, 24h TTL) — pagination cursor.
    - Redis seen_commit_sha (firehose:gh_seen_commit:<sha>, 7d TTL) — dedup.
"""
import asyncio
import logging
import os

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
MAX_PAGES = int(os.getenv("FIREHOSE_MAX_PAGES", 3))    # 100 events × 3 = 300/poll
POLL_TIMEOUT = int(os.getenv("FIREHOSE_POLL_TIMEOUT", 25))


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
    return _run_sync(_poll_github_events_async())


async def _poll_github_events_async():
    if redis_client.get("system:paused"):
        return "paused"

    # Reuse the GithubService rotation pool so firehose load shares the budget.
    from app.services.scanners import TOKEN_PATTERN, GithubService, _is_valid_token
    gh = GithubService()
    token = gh._get_token()
    if not token:
        logger.warning("[Firehose] no GitHub token available")
        return "no_token"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    etag = redis_client.get(ETAG_KEY)
    if etag:
        if isinstance(etag, bytes):
            etag = etag.decode()
        headers["If-None-Match"] = etag

    last_seen_id = redis_client.get(LAST_ID_KEY)
    if isinstance(last_seen_id, bytes):
        last_seen_id = last_seen_id.decode()

    found_total = 0
    new_last_id = None
    results_to_enqueue: list = []

    async with httpx.AsyncClient(timeout=POLL_TIMEOUT, headers=headers) as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(f"{EVENTS_URL}?per_page=100&page={page}")
            except Exception as e:
                logger.warning(f"[Firehose] page={page} fetch failed: {e}")
                break

            if r.status_code == 304:
                logger.debug("[Firehose] 304 not modified")
                return "ok:no_change"
            if r.status_code != 200:
                logger.warning(f"[Firehose] HTTP {r.status_code} on page {page}")
                break

            # Persist ETag from page 1 only (it's the global-stream ETag)
            if page == 1 and "ETag" in r.headers:
                redis_client.setex(ETAG_KEY, 3600, r.headers["ETag"])

            try:
                events = r.json()
            except Exception as e:
                logger.warning(f"[Firehose] JSON parse failed: {e}")
                break

            if not events:
                break

            stop = False
            for ev in events:
                ev_id = ev.get("id")
                if last_seen_id and ev_id == last_seen_id:
                    stop = True
                    break
                if not new_last_id and ev_id:
                    new_last_id = ev_id  # first event of first page = newest
                ev_type = ev.get("type")
                valid_types = {"PushEvent", "IssuesEvent", "PullRequestEvent", "IssueCommentEvent"}

                if ev_type not in valid_types:
                    continue

                payload = ev.get("payload") or {}
                repo_name = (ev.get("repo") or {}).get("name")

                text_blobs = []
                source_meta = {
                    "repo": repo_name,
                    "source_kind": f"github_{ev_type.lower()}"
                }

                if ev_type == "PushEvent":
                    commits = payload.get("commits") or []
                    for commit in commits:
                        commit_url = commit.get("url")
                        if not commit_url:
                            continue
                        commit_sha = commit.get("sha", "")
                        seen_key = f"{SEEN_COMMIT_PREFIX}{commit_sha}"
                        try:
                            if redis_client.exists(seen_key):
                                continue
                            redis_client.setex(seen_key, 7 * 86400, "1")
                        except Exception:
                            pass

                        try:
                            cr = await client.get(commit_url)
                            if cr.status_code != 200:
                                continue
                            commit_data = cr.json()

                            for f in (commit_data.get("files") or []):
                                patch = f.get("patch")
                                if patch:
                                    text_blobs.append(patch)

                            source_meta["commit_sha"] = commit_sha
                            source_meta["commit_url"] = commit_url

                        except Exception as e:
                            logger.debug(f"[Firehose] commit fetch failed: {e}")

                        await asyncio.sleep(0.2)  # gentle pacing per commit fetch

                elif ev_type == "IssuesEvent":
                    issue = payload.get("issue") or {}
                    body = issue.get("body")
                    if body:
                        text_blobs.append(body)
                    source_meta["issue_url"] = issue.get("html_url")

                elif ev_type == "PullRequestEvent":
                    pr = payload.get("pull_request") or {}
                    body = pr.get("body")
                    if body:
                        text_blobs.append(body)
                    source_meta["pr_url"] = pr.get("html_url")

                elif ev_type == "IssueCommentEvent":
                    comment = payload.get("comment") or {}
                    body = comment.get("body")
                    if body:
                        text_blobs.append(body)
                    source_meta["comment_url"] = comment.get("html_url")

                if not text_blobs:
                    continue

                full_text = "\n".join(text_blobs)
                tokens = set(TOKEN_PATTERN.findall(full_text))
                if not tokens:
                    continue

                for tok in tokens:
                    if not _is_valid_token(tok):
                        continue
                    results_to_enqueue.append({
                        "token": tok,
                        "meta": source_meta.copy()
                    })

            if stop:
                break

    if results_to_enqueue:
        found_total = await _save_credentials_async(results_to_enqueue, "firehose_gh_events")
        await _send_log_async(f"⚡ [Firehose] enqueued {found_total} tokens")

    if new_last_id:
        redis_client.setex(LAST_ID_KEY, 86400, new_last_id)

    logger.info(f"[Firehose] poll complete, enqueued {found_total} tokens")
    return f"ok:{found_total}"
