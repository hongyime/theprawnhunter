import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from app.core.circuit_breaker import get_circuit_breaker
from app.infrastructure.scanners.providers import urlscan as urlscan_provider
from app.workers.tasks.scanners.providers import urlscan as urlscan_tasks

_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.sets = {}

    def get(self, key):
        return self.values.get(key)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def sadd(self, key, value):
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.add(value)
        return len(bucket) - before

    def srem(self, key, value):
        bucket = self.sets.setdefault(key, set())
        if value in bucket:
            bucket.remove(value)
            return 1
        return 0


@pytest.fixture
def urlscan_env(monkeypatch):
    redis = MemoryRedis()
    service = urlscan_provider.UrlScanService()
    service.api_key = "test-urlscan-key"
    waits = []

    async def no_sleep(seconds):
        waits.append(seconds)

    async def send_log(_message):
        return None

    async def save_results(results, _source):
        return len(results)

    monkeypatch.setattr(urlscan_tasks, "urlscan", service)
    monkeypatch.setattr(urlscan_tasks, "redis_client", redis)
    monkeypatch.setattr(urlscan_tasks, "_send_log_async", send_log)
    monkeypatch.setattr(urlscan_tasks, "_save_credentials_async", save_results)
    monkeypatch.setattr(urlscan_tasks.asyncio, "sleep", no_sleep)
    get_circuit_breaker("urlscan").reset()
    return redis, service, waits


def install_urlscan_handler(monkeypatch, handler):
    requests = []

    def record_request(request):
        requests.append(request)
        return handler(request)

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(record_request)
        return _HTTPX_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(urlscan_provider.httpx, "AsyncClient", make_client)
    return requests


def reply(request, status=200, payload=None, *, text=None, headers=None):
    if text is not None:
        return httpx.Response(status, text=text, headers=headers, request=request)
    return httpx.Response(status, json=payload, headers=headers, request=request)


def query_records(redis):
    return {
        json.loads(value)["query"]
        for value in redis.smembers(urlscan_tasks.URLSCAN_QUERY_RETRY_KEY)
    }


@pytest.mark.asyncio
async def test_provider_down_saves_search_and_retries_it_on_next_scan(
    monkeypatch, urlscan_env, caplog
):
    redis, _service, _waits = urlscan_env

    def unavailable(request):
        raise httpx.ConnectError("URLScan is unreachable", request=request)

    requests = install_urlscan_handler(monkeypatch, unavailable)
    caplog.set_level(logging.WARNING)

    await urlscan_tasks._scan_urlscan_async(query="custom search")

    assert len(requests) == 4
    assert query_records(redis) == {"custom search"}
    assert "custom search" in caplog.text
    assert "Saved it for the next scan" in caplog.text

    retried = []

    def recovered(request):
        if request.url.path.endswith("/search/"):
            retried.append(request.url.params["q"])
            return reply(request, payload={"results": []})
        return reply(request, text="")

    install_urlscan_handler(monkeypatch, recovered)
    await urlscan_tasks._scan_urlscan_async()

    assert "page.url:*custom search*" in retried
    assert query_records(redis) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "attempts"),
    [
        (401, {"error": "bad key"}, 1),
        (403, {"error": "forbidden"}, 1),
        (404, {"error": "missing"}, 1),
        (422, {"error": "bad query"}, 1),
        (500, {"error": "server error"}, 4),
    ],
)
async def test_provider_http_errors_keep_status_and_search_context(
    monkeypatch, urlscan_env, status, payload, attempts
):
    _redis, service, _waits = urlscan_env
    requests = install_urlscan_handler(
        monkeypatch, lambda request: reply(request, status, payload)
    )

    with pytest.raises(urlscan_provider.UrlScanError, match=f"search 'error query'.*HTTP {status}"):
        await service.search("error query")

    assert len(requests) == attempts


@pytest.mark.asyncio
async def test_error_inside_http_200_is_saved_for_another_try(monkeypatch, urlscan_env, caplog):
    redis, _service, _waits = urlscan_env
    install_urlscan_handler(
        monkeypatch,
        lambda request: reply(request, payload={"error": "search is not allowed"}),
    )
    caplog.set_level(logging.ERROR)

    result = await urlscan_tasks._scan_urlscan_async(query="error reply")

    assert "need another try" in result
    assert query_records(redis) == {"error reply"}
    assert "error reply" in caplog.text
    assert "search is not allowed" in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_waits_then_stops_and_keeps_search(monkeypatch, urlscan_env, caplog):
    redis, _service, waits = urlscan_env
    requests = install_urlscan_handler(
        monkeypatch,
        lambda request: reply(
            request,
            429,
            {"error": "daily limit"},
            headers={"Retry-After": "1"},
        ),
    )
    caplog.set_level(logging.WARNING)

    result = await urlscan_tasks._scan_urlscan_async(query="limited search")

    assert "paused by rate limit" in result
    assert len(requests) == 4
    assert waits.count(1) == 3
    assert query_records(redis) == {"limited search"}
    assert "Stopping this run" in caplog.text


@pytest.mark.asyncio
async def test_empty_results_are_a_logged_success(monkeypatch, urlscan_env, caplog):
    redis, _service, _waits = urlscan_env
    install_urlscan_handler(
        monkeypatch, lambda request: reply(request, payload={"results": []})
    )
    caplog.set_level(logging.INFO)

    result = await urlscan_tasks._scan_urlscan_async(query="empty search")

    assert "Errors" not in result
    assert query_records(redis) == set()
    assert "Search 'empty search' returned 0 hits" in caplog.text
    assert "Search 'empty search' returned 0 credentials" in caplog.text


@pytest.mark.asyncio
async def test_queued_scan_stays_saved_until_dom_is_ready(monkeypatch, urlscan_env):
    redis, _service, _waits = urlscan_env
    scan_id = "queued-scan-1"
    search_item = {
        "_id": scan_id,
        "task": {"time": datetime.now(UTC).isoformat(), "status": "queued"},
        "page": {},
    }
    dom_status = 404
    dom_calls = []

    def handler(request):
        nonlocal dom_status
        if request.url.path.endswith("/search/"):
            if "custom queued" in request.url.params["q"]:
                return reply(request, payload={"results": [search_item]})
            return reply(request, payload={"results": []})
        if request.url.path == f"/dom/{scan_id}":
            dom_calls.append(request)
            return reply(request, dom_status, payload={} if dom_status == 200 else {"error": "not ready"})
        return reply(request, text="")

    install_urlscan_handler(monkeypatch, handler)
    await urlscan_tasks._scan_urlscan_async(query="custom queued")
    assert len(redis.smembers(urlscan_tasks.URLSCAN_SCAN_RETRY_KEY)) == 1
    assert dom_calls == []

    await urlscan_tasks._scan_urlscan_async()
    assert len(dom_calls) == 1
    assert len(redis.smembers(urlscan_tasks.URLSCAN_SCAN_RETRY_KEY)) == 1

    dom_status = 200
    await urlscan_tasks._scan_urlscan_async()
    assert len(dom_calls) == 2
    assert redis.smembers(urlscan_tasks.URLSCAN_SCAN_RETRY_KEY) == set()


@pytest.mark.asyncio
async def test_one_search_failure_does_not_stop_other_searches(monkeypatch, urlscan_env, caplog):
    redis, _service, _waits = urlscan_env
    queries = []

    def handler(request):
        query = request.url.params["q"]
        queries.append(query)
        if query == "page.url:*api.telegram.org/bot*":
            return reply(request, 401, {"error": "bad key"})
        return reply(request, payload={"results": []})

    install_urlscan_handler(monkeypatch, handler)
    caplog.set_level(logging.ERROR)

    result = await urlscan_tasks._scan_urlscan_async()

    assert len(queries) == 6
    assert "page.url:*api.telegram.org/bot*" in queries
    assert len(set(queries)) == 6
    assert query_records(redis) == {"api.telegram.org/bot"}
    assert "continuing with the other searches" in caplog.text
    assert "need another try" in result


@pytest.mark.asyncio
async def test_missing_api_key_logs_and_returns_without_a_request(monkeypatch, urlscan_env, caplog):
    redis, service, _waits = urlscan_env
    service.api_key = ""
    requests = install_urlscan_handler(monkeypatch, lambda request: request)
    caplog.set_level(logging.WARNING)

    result = await urlscan_tasks._scan_urlscan_async(query="retry without key")

    assert result == "URLScan API key missing; scan skipped"
    assert requests == []
    assert query_records(redis) == {"retry without key"}
    assert "API key is missing or empty" in caplog.text
