import logging
from types import SimpleNamespace

import httpx
import pytest

from app.core.circuit_breaker import get_circuit_breaker
from app.infrastructure.scanners.providers import shodan as shodan_provider
from app.workers.tasks.scanners.providers import shodan as shodan_tasks


class MemoryRedis:
    """In-memory Redis for the real Shodan worker's pause and retry state."""

    def __init__(self):
        self.values = {}
        self.sets = {}

    def get(self, key):
        return self.values.get(key)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def sadd(self, key, value):
        values = self.sets.setdefault(key, set())
        old_count = len(values)
        values.add(value)
        return len(values) - old_count

    def srem(self, key, value):
        values = self.sets.setdefault(key, set())
        if value in values:
            values.remove(value)
            return 1
        return 0


@pytest.fixture
def shodan_env(monkeypatch):
    redis = MemoryRedis()
    service = shodan_provider.ShodanService()
    service.api_key = "test-shodan-key"
    waits = []

    async def no_sleep(seconds):
        waits.append(seconds)

    async def send_log(_message):
        return None

    async def save_results(results, _source):
        return len(results)

    monkeypatch.setattr(shodan_tasks, "redis_client", redis)
    monkeypatch.setattr(shodan_tasks, "shodan", service)
    monkeypatch.setattr(shodan_tasks, "SHODAN_DEFAULT_QUERIES", [])
    monkeypatch.setattr(shodan_tasks, "_send_log_async", send_log)
    monkeypatch.setattr(shodan_tasks, "_save_credentials_async", save_results)
    monkeypatch.setattr(shodan_tasks.asyncio, "sleep", no_sleep)
    get_circuit_breaker("shodan").reset()
    return SimpleNamespace(redis=redis, service=service, waits=waits)


def install_shodan_reply(monkeypatch, handler):
    requests = []

    def record_request(request):
        requests.append(request)
        return handler(request)

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(record_request)
        return httpx.AsyncClient(*args, **kwargs)

    monkeypatch.setattr(shodan_provider, "get_async_http_client", make_client)
    return requests


def json_reply(request, status, payload, headers=None):
    return httpx.Response(status, json=payload, headers=headers, request=request)


@pytest.mark.asyncio
async def test_provider_down_logs_search_and_retries_saved_custom_search(
    monkeypatch, shodan_env, caplog
):
    requests = []

    def unavailable(request):
        requests.append(request)
        raise httpx.ConnectError("Shodan is unreachable", request=request)

    install_shodan_reply(monkeypatch, unavailable)
    caplog.set_level(logging.WARNING)

    first_result = await shodan_tasks._scan_shodan_async(query="custom search")

    assert first_result == " (Errors: 1)"
    assert len(requests) == 4
    assert shodan_env.waits == [2, 4, 8, 1]
    assert "custom search" in caplog.text
    assert "next scheduled scan" in caplog.text
    saved_retry = shodan_env.redis.smembers(shodan_tasks.SHODAN_RETRY_QUERY_KEY)
    assert len(saved_retry) == 1

    def recovered(request):
        requests.append(request)
        return json_reply(request, 200, {"matches": [], "total": 0})

    install_shodan_reply(monkeypatch, recovered)
    second_result = await shodan_tasks._scan_shodan_async()

    assert second_result == ""
    assert requests[-1].url.params["query"] == "custom search"
    assert shodan_env.redis.smembers(shodan_tasks.SHODAN_RETRY_QUERY_KEY) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (401, {"error": "Invalid API key"}),
        (403, {"error": "Forbidden"}),
        (404, {"error": "Not found"}),
        (422, {"error": "Invalid query"}),
        (500, {"error": "Shodan server failed"}),
    ],
)
async def test_http_error_for_one_search_is_logged_and_next_search_runs(
    monkeypatch, shodan_env, caplog, status, payload
):
    monkeypatch.setattr(shodan_tasks, "SHODAN_DEFAULT_QUERIES", ["bad search", "good search"])
    seen_queries = []

    def handler(request):
        query = request.url.params["query"]
        seen_queries.append(query)
        if query == "bad search":
            return json_reply(request, status, payload)
        return json_reply(request, 200, {"matches": [], "total": 0})

    install_shodan_reply(monkeypatch, handler)
    caplog.set_level(logging.INFO)

    result = await shodan_tasks._scan_shodan_async()

    assert result == " (Errors: 1)"
    assert "good search" in seen_queries
    assert "bad search" in caplog.text
    assert "continuing with the other searches" in caplog.text


@pytest.mark.asyncio
async def test_error_field_in_success_reply_is_not_reported_as_empty(monkeypatch, shodan_env, caplog):
    monkeypatch.setattr(shodan_tasks, "SHODAN_DEFAULT_QUERIES", ["error in 200"])
    install_shodan_reply(
        monkeypatch,
        lambda request: json_reply(request, 200, {"error": "Search is not allowed"}),
    )
    caplog.set_level(logging.INFO)

    result = await shodan_tasks._scan_shodan_async()

    assert result == " (Errors: 1)"
    assert "error in 200" in caplog.text
    assert "Search is not allowed" in caplog.text
    assert "returned no matches" not in caplog.text
    assert len(shodan_env.redis.smembers(shodan_tasks.SHODAN_RETRY_QUERY_KEY)) == 1


@pytest.mark.asyncio
async def test_empty_results_are_logged_as_a_success(monkeypatch, shodan_env, caplog):
    monkeypatch.setattr(shodan_tasks, "SHODAN_DEFAULT_QUERIES", ["empty search"])
    install_shodan_reply(
        monkeypatch,
        lambda request: json_reply(request, 200, {"matches": [], "total": 0}),
    )
    caplog.set_level(logging.INFO)

    result = await shodan_tasks._scan_shodan_async()

    assert result == ""
    assert "empty search" in caplog.text
    assert "returned no matches" in caplog.text
    assert shodan_env.redis.smembers(shodan_tasks.SHODAN_RETRY_QUERY_KEY) == set()


@pytest.mark.asyncio
async def test_search_uses_only_the_first_100_results(monkeypatch, shodan_env, caplog):
    token = f"12345678:{'Z' * 35}"
    matches = [
        {"ip_str": f"192.0.2.{index}", "port": 443, "data": token}
        for index in range(1, 102)
    ]
    requests = install_shodan_reply(
        monkeypatch,
        lambda request: json_reply(request, 200, {"matches": matches, "total": len(matches)}),
    )
    caplog.set_level(logging.WARNING)

    results = await shodan_env.service.search("page limit")

    assert len(results) == 100
    assert requests[0].url.params["page"] == "1"
    assert "first 100 matches" in caplog.text


@pytest.mark.asyncio
async def test_active_probe_failure_is_a_warning_with_search_and_host(
    monkeypatch, shodan_env, caplog
):
    original_async_client = httpx.AsyncClient

    def handler(request):
        if request.url.host == "api.shodan.io":
            return json_reply(
                request,
                200,
                {
                    "matches": [
                        {"ip_str": "192.0.2.50", "port": 80, "data": ""}
                    ],
                    "total": 1,
                },
            )
        raise httpx.ConnectError("host cannot be reached", request=request)

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(shodan_provider, "get_async_http_client", make_client)
    monkeypatch.setattr(shodan_provider.httpx, "AsyncClient", make_client)
    caplog.set_level(logging.WARNING)

    result = await shodan_tasks._scan_shodan_async(query="probe query")

    assert result == ""
    assert "probe query" in caplog.text
    assert "192.0.2.50:80" in caplog.text
    assert "ConnectError" in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_waits_once_then_stops_without_sending_other_searches(
    monkeypatch, shodan_env, caplog
):
    monkeypatch.setattr(shodan_tasks, "SHODAN_DEFAULT_QUERIES", ["limited search", "later search"])
    requests = install_shodan_reply(
        monkeypatch,
        lambda request: json_reply(
            request,
            429,
            {"error": "Too many requests"},
            headers={"Retry-After": "3"},
        ),
    )
    caplog.set_level(logging.WARNING)

    result = await shodan_tasks._scan_shodan_async()

    assert result == " (Errors: 1)"
    assert len(requests) == 2
    assert shodan_env.waits == [3]
    assert requests[0].url.params["query"] == "limited search"
    assert "stopping this run" in caplog.text
    assert "later search" not in [request.url.params["query"] for request in requests]
    assert len(shodan_env.redis.smembers(shodan_tasks.SHODAN_RETRY_QUERY_KEY)) == 1


@pytest.mark.asyncio
async def test_exhausted_query_credits_stop_the_run(monkeypatch, shodan_env, caplog):
    monkeypatch.setattr(shodan_tasks, "SHODAN_DEFAULT_QUERIES", ["credit-limited search", "later search"])
    requests = install_shodan_reply(
        monkeypatch,
        lambda request: json_reply(
            request,
            403,
            {"error": "Query credits exhausted"},
        ),
    )
    caplog.set_level(logging.WARNING)

    result = await shodan_tasks._scan_shodan_async()

    assert result == " (Errors: 1)"
    assert len(requests) == 1
    assert "Query credits exhausted" in caplog.text
    assert "stopping this run" in caplog.text


@pytest.mark.asyncio
async def test_missing_or_blank_key_is_clear_and_custom_search_stays_saved(
    monkeypatch, shodan_env, caplog
):
    shodan_env.service.api_key = "   "
    requests = install_shodan_reply(
        monkeypatch,
        lambda request: pytest.fail(f"Shodan request should not be sent: {request.url}"),
    )
    caplog.set_level(logging.WARNING)

    result = await shodan_tasks._scan_shodan_async(query="custom without key")

    assert result == "Shodan API key missing"
    assert requests == []
    assert "API key is missing or empty" in caplog.text
    assert len(shodan_env.redis.smembers(shodan_tasks.SHODAN_RETRY_QUERY_KEY)) == 1


@pytest.mark.asyncio
async def test_c2_scan_keeps_running_after_one_search_fails(monkeypatch, shodan_env, caplog):
    request_number = 0

    def handler(request):
        nonlocal request_number
        request_number += 1
        if request_number == 1:
            return json_reply(request, 404, {"error": "One C2 query failed"})
        return json_reply(request, 200, {"matches": [], "total": 0})

    requests = install_shodan_reply(monkeypatch, handler)
    caplog.set_level(logging.WARNING)

    result = await shodan_tasks._scan_shodan_c2_async()

    assert result.endswith("(Errors: 1)")
    assert len(requests) > 1
    assert "Shodan-C2" in caplog.text
    assert "continuing with the other searches" in caplog.text

    second_requests = install_shodan_reply(
        monkeypatch,
        lambda request: json_reply(request, 200, {"matches": [], "total": 0}),
    )
    await shodan_tasks._scan_shodan_c2_async()

    assert second_requests[0].url.params["query"] == requests[0].url.params["query"]
