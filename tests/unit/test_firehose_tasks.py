import hashlib
import logging
import time
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.workers.tasks import firehose_tasks


class MemoryRedis:
    """Simple in-memory Redis reply for the real firehose task."""

    def __init__(self):
        self.values = {}
        self.request_count = 0
        self.slot_reply = [1, 0]

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, _ttl, value):
        self.values[key] = str(value)
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = str(value)
        return True

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                deleted += 1
        return deleted

    def exists(self, key):
        return key in self.values

    def eval(self, _script, _key_count, _key, _window, _budget, _member):
        self.request_count += 1
        return self.slot_reply


@pytest.fixture
def firehose_env(monkeypatch):
    from app.workers.tasks.scanners import common as scanner_common

    redis = MemoryRedis()
    monkeypatch.setattr(firehose_tasks, "redis_client", redis)
    monkeypatch.setattr(scanner_common, "redis_client", redis)
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "test-github-token", raising=False)
    monkeypatch.setattr(settings, "GITHUB_TOKENS", None, raising=False)
    monkeypatch.setattr(firehose_tasks, "_LOCAL_PAUSE_UNTIL", 0.0)
    redis.values[firehose_tasks.LAST_ID_KEY] = "prior-event"
    return redis


def install_github_reply(monkeypatch, handler):
    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(*args, **kwargs)

    monkeypatch.setattr(
        firehose_tasks,
        "httpx",
        SimpleNamespace(AsyncClient=make_client),
    )


def push_event(event_id, *commits):
    return {
        "id": event_id,
        "type": "PushEvent",
        "repo": {"name": "owner/repo"},
        "payload": {"commits": list(commits)},
    }


@pytest.mark.asyncio
async def test_events_provider_down_keeps_marker_and_logs_retry(monkeypatch, firehose_env, caplog):
    def handler(request):
        raise httpx.ConnectError("GitHub is unreachable", request=request)

    install_github_reply(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    result = await firehose_tasks._poll_github_events_async()

    assert result == "partial:0"
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"
    assert "GitHub is unreachable" in caplog.text
    assert "tried again next poll" in caplog.text


@pytest.mark.asyncio
async def test_events_http_error_keeps_marker_and_logs_reply(monkeypatch, firehose_env, caplog):
    def handler(_request):
        return httpx.Response(500, json={"message": "GitHub server error"})

    install_github_reply(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    result = await firehose_tasks._poll_github_events_async()

    assert result == "partial:0"
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"
    assert "HTTP 500" in caplog.text
    assert "GitHub server error" in caplog.text


@pytest.mark.asyncio
async def test_empty_events_are_logged_without_moving_marker(monkeypatch, firehose_env, caplog):
    def handler(_request):
        return httpx.Response(200, json=[])

    install_github_reply(monkeypatch, handler)
    caplog.set_level(logging.INFO, logger=firehose_tasks.logger.name)

    result = await firehose_tasks._poll_github_events_async()

    assert result == "ok:0"
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"
    assert "no more events" in caplog.text


@pytest.mark.asyncio
async def test_events_rate_limit_saves_wait_and_skips_next_request(monkeypatch, firehose_env, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(time.time() + 120),
            },
        )

    install_github_reply(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    result = await firehose_tasks._poll_github_events_async()
    next_result = await firehose_tasks._poll_github_events_async()

    assert result == "partial:0"
    assert next_result == "rate_limited"
    assert len(requests) == 1
    assert float(firehose_env.get(firehose_tasks.RATE_LIMIT_KEY)) > time.time()
    assert "API rate limit exceeded" in caplog.text
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"


@pytest.mark.asyncio
async def test_hourly_request_limit_stops_without_calling_github(monkeypatch, firehose_env, caplog):
    firehose_env.slot_reply = [0, 35]

    def handler(_request):
        raise AssertionError("GitHub must not be called after the hourly limit is reached")

    install_github_reply(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    result = await firehose_tasks._poll_github_events_async()

    assert result == "partial:0"
    assert firehose_env.request_count == 1
    assert float(firehose_env.get(firehose_tasks.RATE_LIMIT_KEY)) > time.time()
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"
    assert "hourly request budget" in caplog.text


@pytest.mark.asyncio
async def test_commit_http_error_is_logged_and_left_for_next_poll(monkeypatch, firehose_env, caplog):
    commit_url = "https://api.github.com/repos/owner/repo/commits/sha-bad"

    def handler(request):
        if request.url.path == "/events":
            return httpx.Response(200, json=[push_event("fresh-event", {"sha": "sha-bad", "url": commit_url})])
        return httpx.Response(500, json={"message": "commit service failed"})

    install_github_reply(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    result = await firehose_tasks._poll_github_events_async()

    assert result == "partial:0"
    assert not firehose_env.exists(f"{firehose_tasks.SEEN_COMMIT_PREFIX}sha-bad")
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"
    assert "HTTP 500" in caplog.text
    assert "commit service failed" in caplog.text
    assert "tried again next poll" in caplog.text


@pytest.mark.asyncio
async def test_failed_commit_does_not_stop_other_commits_and_is_retried(monkeypatch, firehose_env, caplog):
    failed_url = "https://api.github.com/repos/owner/repo/commits/sha-retry"
    good_url = "https://api.github.com/repos/owner/repo/commits/sha-good"
    failed_calls = 0
    good_calls = 0

    def handler(request):
        nonlocal failed_calls, good_calls
        if request.url.path == "/events":
            return httpx.Response(
                200,
                json=[
                    push_event(
                        "fresh-event",
                        {"sha": "sha-retry", "url": failed_url},
                        {"sha": "sha-good", "url": good_url},
                    )
                ],
            )
        if str(request.url) == failed_url:
            failed_calls += 1
            if failed_calls == 1:
                raise httpx.ConnectError("temporary commit outage", request=request)
            return httpx.Response(200, json={"files": []})
        if str(request.url) == good_url:
            good_calls += 1
            return httpx.Response(200, json={"files": []})
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    install_github_reply(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    first = await firehose_tasks._poll_github_events_async()
    assert first == "partial:0"
    assert failed_calls == 1
    assert good_calls == 1
    assert not firehose_env.exists(f"{firehose_tasks.SEEN_COMMIT_PREFIX}sha-retry")
    assert firehose_env.exists(f"{firehose_tasks.SEEN_COMMIT_PREFIX}sha-good")
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"

    second = await firehose_tasks._poll_github_events_async()
    assert second == "ok:0"
    assert failed_calls == 2
    assert good_calls == 1
    assert firehose_env.exists(f"{firehose_tasks.SEEN_COMMIT_PREFIX}sha-retry")
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "fresh-event"
    assert "temporary commit outage" in caplog.text


@pytest.mark.asyncio
async def test_commit_rate_limit_stops_cleanly_and_keeps_work(monkeypatch, firehose_env, caplog):
    first_url = "https://api.github.com/repos/owner/repo/commits/sha-limited"
    later_url = "https://api.github.com/repos/owner/repo/commits/sha-later"
    commit_requests = []

    def handler(request):
        if request.url.path == "/events":
            return httpx.Response(
                200,
                json=[
                    push_event(
                        "fresh-event",
                        {"sha": "sha-limited", "url": first_url},
                        {"sha": "sha-later", "url": later_url},
                    )
                ],
            )
        commit_requests.append(request.url)
        return httpx.Response(
            429,
            json={"message": "secondary rate limit"},
            headers={"Retry-After": "90"},
        )

    install_github_reply(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    result = await firehose_tasks._poll_github_events_async()

    assert result == "partial:0"
    assert commit_requests == [httpx.URL(first_url)]
    assert not firehose_env.exists(f"{firehose_tasks.SEEN_COMMIT_PREFIX}sha-limited")
    assert not firehose_env.exists(f"{firehose_tasks.SEEN_COMMIT_PREFIX}sha-later")
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"
    assert float(firehose_env.get(firehose_tasks.RATE_LIMIT_KEY)) > time.time()
    assert "secondary rate limit" in caplog.text


@pytest.mark.asyncio
async def test_missing_login_token_logs_and_returns_without_request(monkeypatch, firehose_env, caplog):
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "", raising=False)
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    result = await firehose_tasks._poll_github_events_async()

    assert result == "no_token"
    assert "no GitHub login token is set" in caplog.text
    assert firehose_env.request_count == 0


@pytest.mark.asyncio
async def test_queue_failure_clears_shared_mark_and_retries_result(monkeypatch, firehose_env, caplog):
    from app.workers.tasks import flow_tasks

    token = "123456789:" + ("A" * 34) + "z"
    commit_url = "https://api.github.com/repos/owner/repo/commits/sha-token"
    queue_calls = []

    def handler(request):
        if request.url.path == "/events":
            return httpx.Response(
                200,
                json=[push_event("fresh-event", {"sha": "sha-token", "url": commit_url})],
            )
        return httpx.Response(200, json={"files": [{"patch": token}]})

    async def queue_entries(entries):
        entries = list(entries)
        queue_calls.append(entries)
        if len(queue_calls) == 1:
            raise RuntimeError("validation queue is down")
        return len(entries)

    class QuietBroadcaster:
        async def send_log(self, _message):
            return True

    install_github_reply(monkeypatch, handler)
    monkeypatch.setattr("app.services.validation_queue_srv.enqueue", queue_entries)
    monkeypatch.setattr(flow_tasks, "get_broadcaster", lambda: QuietBroadcaster())
    caplog.set_level(logging.WARNING, logger=firehose_tasks.logger.name)

    first = await firehose_tasks._poll_github_events_async()
    assert first == "partial:0"
    assert len(queue_calls) == 1
    assert not firehose_env.exists(f"{firehose_tasks.SEEN_COMMIT_PREFIX}sha-token")
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "prior-event"

    second = await firehose_tasks._poll_github_events_async()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    assert second == "ok:1"
    assert len(queue_calls) == 2
    assert firehose_env.exists(f"{firehose_tasks.SEEN_COMMIT_PREFIX}sha-token")
    assert not firehose_env.exists(f"{firehose_tasks.PENDING_VALIDATION_PREFIX}{token_hash}")
    assert firehose_env.get(firehose_tasks.LAST_ID_KEY) == "fresh-event"
    assert "validation queue is down" in caplog.text
