from collections import defaultdict, deque
from types import SimpleNamespace

import pytest


class _MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    @staticmethod
    def expire(_key, _seconds):
        return True


@pytest.fixture
def netlas_harness(monkeypatch):
    import netlas as netlas_sdk

    from app.infrastructure.scanners.providers.netlas import NetlasService
    from app.workers.tasks.scanners.providers import netlas as netlas_task

    redis = _MemoryRedis()
    responses = defaultdict(deque)
    calls = []
    events = []
    logs = []
    saved = []
    waits = []

    class FakeNetlas:
        def __init__(self, *, api_key):
            assert api_key == "fake-netlas-key"

        def query(self, *, query, datatype, page, throttling, retry):
            calls.append(
                {
                    "query": query,
                    "datatype": datatype,
                    "page": page,
                    "throttling": throttling,
                    "retry": retry,
                }
            )
            events.append(("request", query, throttling, retry))
            outcome = responses[query].popleft() if responses[query] else {"items": []}
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    async def send_log(message):
        logs.append(message)

    async def save_results(results, source):
        saved.extend((source, result) for result in results)
        return len(results)

    async def fake_sleep(seconds):
        waits.append(seconds)
        events.append(("sleep", seconds))

    service = NetlasService()
    service.keys = [(1, "fake-netlas-key")]
    monkeypatch.setattr(netlas_task, "NETLAS_QUERIES", list(netlas_task.NETLAS_QUERIES))
    monkeypatch.setattr(netlas_task, "NETLAS_RESULTS_PER_RUN", netlas_task.NETLAS_RESULTS_PER_RUN)
    monkeypatch.setattr(service, "_redis", lambda: redis)
    monkeypatch.setattr(netlas_sdk, "Netlas", FakeNetlas)
    monkeypatch.setattr(netlas_task, "netlas_srv", service)
    monkeypatch.setattr(netlas_task, "redis_client", redis)
    monkeypatch.setattr(netlas_task, "_send_log_async", send_log)
    monkeypatch.setattr(netlas_task, "_save_credentials_async", save_results)
    monkeypatch.setattr(netlas_task.asyncio, "sleep", fake_sleep)

    def reply(query, outcome):
        responses[query].append(outcome)

    return SimpleNamespace(
        task=netlas_task,
        service=service,
        redis=redis,
        calls=calls,
        events=events,
        logs=logs,
        saved=saved,
        waits=waits,
        reply=reply,
    )


def _fake_token(number):
    return f"{12345678 + number}:{'Z' * 35}"


@pytest.mark.asyncio
async def test_custom_query_uses_real_provider_and_only_sends_that_query(netlas_harness):
    h = netlas_harness
    h.task.NETLAS_QUERIES = ["default query"]
    h.reply("caller query", {"items": []})

    await h.task._scan_netlas_async("caller query")

    assert [call["query"] for call in h.calls] == ["caller query"]
    assert h.calls[0] == {
        "query": "caller query",
        "datatype": "response",
        "page": 0,
        "throttling": False,
        "retry": 0,
    }


@pytest.mark.asyncio
async def test_provider_down_logs_failure_continues_and_retries_on_next_scan(netlas_harness):
    from requests.exceptions import ConnectionError

    h = netlas_harness
    h.task.NETLAS_QUERIES = ["first search", "second search"]
    h.reply("first search", ConnectionError("connection refused"))
    h.reply("first search", {"items": []})

    await h.task._scan_netlas_async()

    assert [call["query"] for call in h.calls] == ["first search", "second search"]
    assert any(
        "first search" in line
        and "ConnectionError" in line
        and "Continuing with the next search" in line
        and "tried again on the next scan" in line
        for line in h.logs
    )

    await h.task._scan_netlas_async()

    assert [call["query"] for call in h.calls] == [
        "first search",
        "second search",
        "first search",
        "second search",
    ]
    assert sum(int(value) for key, value in h.redis.values.items() if key.startswith("netlas:daily_requests")) == 4


@pytest.mark.asyncio
async def test_rate_limit_waits_for_requested_time_before_next_search(netlas_harness):
    from netlas.exception import ThrottlingError

    h = netlas_harness
    h.task.NETLAS_QUERIES = ["limited search", "later search"]
    h.reply("limited search", ThrottlingError(retry_after=9))

    await h.task._scan_netlas_async()

    assert [call["query"] for call in h.calls] == ["limited search", "later search"]
    assert h.events == [
        ("request", "limited search", False, 0),
        ("sleep", 9),
        ("request", "later search", False, 0),
        ("sleep", 1),
    ]
    assert any("limited search" in line and "Waiting 9 seconds" in line for line in h.logs)


@pytest.mark.asyncio
async def test_rejected_api_key_warns_and_stops_cleanly(netlas_harness):
    from netlas.exception import APIError

    h = netlas_harness
    h.task.NETLAS_QUERIES = ["bad key search", "later search"]
    h.reply(
        "bad key search",
        APIError("Invalid API key", "unauthorized", "The key was rejected"),
    )

    result = await h.task._scan_netlas_async()

    assert result.startswith("Netlas scan finished")
    assert [call["query"] for call in h.calls] == ["bad key search"]
    assert any(
        "⚠️ [Netlas]" in line
        for line in h.logs
        if "API key was not accepted" in line
    )


@pytest.mark.asyncio
async def test_missing_api_key_warns_and_sends_no_request(netlas_harness):
    h = netlas_harness
    h.service.keys = []
    h.task.NETLAS_QUERIES = ["search that must not be sent"]

    result = await h.task._scan_netlas_async()

    assert result == "No Netlas API key configured"
    assert h.calls == []
    assert any("No API key is set" in line for line in h.logs)


@pytest.mark.asyncio
async def test_empty_results_are_logged_as_a_success(netlas_harness):
    h = netlas_harness
    h.task.NETLAS_QUERIES = ["empty search"]
    h.reply("empty search", {"items": []})

    result = await h.task._scan_netlas_async()

    assert "Ran 1 searches" in result
    assert "Errors:" not in result
    assert any("Search returned 0 results" in line for line in h.logs)


@pytest.mark.asyncio
async def test_request_per_run_limit_is_enforced(netlas_harness):
    h = netlas_harness
    h.service.MAX_REQUESTS_PER_RUN = 2
    h.task.NETLAS_QUERIES = ["one", "two", "three"]

    await h.task._scan_netlas_async()

    assert [call["query"] for call in h.calls] == ["one", "two"]
    assert any("Request-per-run limit (2) reached" in line for line in h.logs)


@pytest.mark.asyncio
async def test_provider_and_scan_limit_extracted_results_per_run(netlas_harness):
    h = netlas_harness
    h.task.NETLAS_QUERIES = ["many tokens"]
    h.task.NETLAS_RESULTS_PER_RUN = 2
    body = "\n".join(_fake_token(index) for index in range(3))
    h.reply("many tokens", {"items": [{"data": {"http": {"body": body}}}]})

    await h.task._scan_netlas_async()

    assert len(h.saved) == 2
    assert any("Results-per-run limit (2) reached" in line for line in h.logs)


@pytest.mark.asyncio
async def test_provider_reads_at_most_twenty_response_items_per_request(netlas_harness):
    h = netlas_harness
    h.task.NETLAS_QUERIES = ["many response items"]
    items = [
        {"data": {"http": {"body": _fake_token(index)}}}
        for index in range(25)
    ]
    h.reply("many response items", {"items": items})

    await h.task._scan_netlas_async()

    assert len(h.saved) == 20
