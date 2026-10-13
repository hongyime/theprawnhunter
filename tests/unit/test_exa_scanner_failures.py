from types import SimpleNamespace

import httpx
import pytest

from app.infrastructure.scanners.providers import exa as exa_provider
from app.workers.tasks.scanners.providers import exa as exa_task


class _MemoryRedis:
    def __init__(self, lists=None):
        self.lists = {key: list(values) for key, values in (lists or {}).items()}

    def get(self, key):
        return None

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lrem(self, key, count, value):
        values = self.lists.get(key, [])
        kept = []
        removed = 0
        for item in values:
            if item == value and (count == 0 or removed < count):
                removed += 1
            else:
                kept.append(item)
        self.lists[key] = kept
        return removed


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = {"results": []} if payload is None else payload
        self.headers = headers or {}
        self.text = str(self.payload)

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Client:
    def __init__(self, responder, calls):
        self.responder = responder
        self.calls = calls

    async def post(self, url, headers, json):
        self.calls.append({"url": url, "headers": headers, "payload": json})
        return self.responder(json["query"], headers, json)


class _ClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *args):
        return False


def _set_keys(monkeypatch, first="exa-key", second=None, third=None):
    monkeypatch.setattr(exa_provider.settings, "EXA_API_KEY", first)
    monkeypatch.setattr(exa_provider.settings, "EXA_API_KEY_2", second)
    monkeypatch.setattr(exa_provider.settings, "EXA_API_KEY_3", third)


def _prepare(monkeypatch, responder, queries, *, pending=None):
    calls = []
    redis = _MemoryRedis({exa_task.EXA_RETRY_QUEUE: pending or []})
    saved_results = []
    statuses = []
    sleeps = []
    client = _Client(responder, calls)

    monkeypatch.setattr(
        exa_provider,
        "get_async_http_client",
        lambda **kwargs: _ClientContext(client),
    )
    monkeypatch.setattr(exa_task, "redis_client", redis)
    monkeypatch.setattr(exa_task, "EXA_DEFAULT_QUERIES", queries)

    async def save_results(results, source):
        saved_results.extend((source, item) for item in results)
        return len(results)

    async def send_status(message):
        statuses.append(message)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(exa_task, "_save_credentials_async", save_results)
    monkeypatch.setattr(exa_task, "_send_log_async", send_status)
    monkeypatch.setattr(exa_task, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(exa_provider, "asyncio", SimpleNamespace(sleep=sleep))
    return redis, calls, saved_results, statuses, sleeps


def _response(status=200, payload=None, headers=None):
    return _Response(status, payload, headers)


@pytest.mark.asyncio
async def test_provider_down_keeps_one_off_search_for_next_scan(monkeypatch, caplog):
    _set_keys(monkeypatch)
    attempts = 0

    def responder(query, headers, payload):
        nonlocal attempts
        attempts += 1
        if attempts <= exa_provider.MAX_ATTEMPTS:
            raise httpx.ConnectError("Exa is unreachable")
        return _response()

    redis, calls, _, _, sleeps = _prepare(monkeypatch, responder, ["unused-default"])

    first_run = await exa_task._scan_exa_async("one-off search")

    assert "Kept 1 failed search" in first_run
    assert redis.lists[exa_task.EXA_RETRY_QUEUE] == ["one-off search"]
    assert [call["payload"]["query"] for call in calls] == ["one-off search"] * 4
    assert sleeps == [2, 4, 8]
    assert "one-off search" in caplog.text
    assert "continuing with other searches" in caplog.text

    second_run = await exa_task._scan_exa_async("one-off search")

    assert "Saved 0 new credentials" in second_run
    assert redis.lists[exa_task.EXA_RETRY_QUEUE] == []
    assert len(calls) == 5


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (404, {"error": "not found"}, "HTTP 404"),
        (422, {"error": "bad query"}, "HTTP 422"),
        (500, {"error": "server down"}, "after 4 requests"),
        (200, {"error": "query rejected"}, "error in HTTP 200 reply"),
    ],
)
@pytest.mark.asyncio
async def test_provider_errors_raise_with_search_context(monkeypatch, caplog, status, payload, expected):
    _set_keys(monkeypatch)
    _, calls, _, _, _ = _prepare(
        monkeypatch,
        lambda query, headers, body: _response(status, payload),
        ["unused-default"],
    )

    with pytest.raises(exa_provider.ExaSearchFailure, match=expected):
        await exa_task.exa_srv.search("bad query", raise_on_error=True)

    assert all(call["payload"]["query"] == "bad query" for call in calls)
    assert "bad query" in caplog.text
    if status == 500:
        assert len(calls) == 4


@pytest.mark.asyncio
async def test_empty_results_are_logged_as_complete_and_clear_saved_search(monkeypatch, caplog):
    _set_keys(monkeypatch)
    redis, calls, _, _, _ = _prepare(
        monkeypatch,
        lambda query, headers, payload: _response(200, {"results": []}),
        ["no matches"],
        pending=["no matches"],
    )

    result = await exa_task._scan_exa_async()

    assert "Saved 0 new credentials" in result
    assert redis.lists[exa_task.EXA_RETRY_QUEUE] == []
    assert len(calls) == 1
    assert calls[0]["payload"]["numResults"] == 25
    assert calls[0]["payload"]["contents"]["text"]["maxCharacters"] == 10000
    assert "page" not in calls[0]["payload"]
    assert "no matches" in caplog.text
    assert "completed with no token matches" in caplog.text


@pytest.mark.asyncio
async def test_one_failed_search_is_kept_while_later_searches_run(monkeypatch, caplog):
    _set_keys(monkeypatch)

    def responder(query, headers, payload):
        if query == "bad search":
            return _response(404, {"error": "not found"})
        return _response(200, {"results": []})

    redis, calls, _, _, sleeps = _prepare(monkeypatch, responder, ["bad search", "good search"])

    result = await exa_task._scan_exa_async()

    assert "Kept 1 failed search" in result
    assert [call["payload"]["query"] for call in calls] == ["bad search", "good search"]
    assert redis.lists[exa_task.EXA_RETRY_QUEUE] == ["bad search"]
    assert sleeps == [2]
    assert "bad search" in caplog.text
    assert "continuing with other searches" in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_waits_then_stops_and_keeps_current_and_later_searches(monkeypatch, caplog):
    _set_keys(monkeypatch)
    redis, calls, _, statuses, sleeps = _prepare(
        monkeypatch,
        lambda query, headers, payload: _response(
            429, {"error": "too many requests"}, {"Retry-After": "1"}
        ),
        ["limited search", "later search"],
    )

    result = await exa_task._scan_exa_async()

    assert "stopped this run cleanly" in statuses[1]
    assert redis.lists[exa_task.EXA_RETRY_QUEUE] == ["limited search", "later search"]
    assert [call["payload"]["query"] for call in calls] == ["limited search"] * 4
    assert sleeps == [1, 1, 1]
    assert "limited search" in caplog.text
    assert "rate limit" in caplog.text.lower()


@pytest.mark.asyncio
async def test_quota_exhaustion_stops_and_keeps_all_searches(monkeypatch, caplog):
    _set_keys(monkeypatch)
    redis, calls, _, statuses, _ = _prepare(
        monkeypatch,
        lambda query, headers, payload: _response(402, {"error": "quota exhausted"}),
        ["quota search", "later search"],
    )

    await exa_task._scan_exa_async()

    assert len(calls) == 1
    assert redis.lists[exa_task.EXA_RETRY_QUEUE] == ["quota search", "later search"]
    assert "quota is exhausted" in caplog.text
    assert "stopped this run cleanly" in statuses[1]


@pytest.mark.asyncio
async def test_missing_or_blank_keys_warn_and_keep_searches(monkeypatch, caplog):
    _set_keys(monkeypatch, None, "", "   ")
    redis, calls, _, statuses, _ = _prepare(
        monkeypatch,
        lambda query, headers, payload: _response(),
        ["first search", "later search"],
    )

    await exa_task._scan_exa_async()

    assert calls == []
    assert redis.lists[exa_task.EXA_RETRY_QUEUE] == ["first search", "later search"]
    assert "no non-empty Exa API key" in caplog.text
    assert "no usable key" in statuses[1]


@pytest.mark.asyncio
async def test_rejected_keys_are_rotated_then_searches_are_kept(monkeypatch, caplog):
    _set_keys(monkeypatch, "key-one", "key-two")
    monkeypatch.setattr(exa_provider.random, "choice", lambda keys: keys[0])

    def responder(query, headers, payload):
        status = 401 if headers["x-api-key"] == "key-one" else 403
        return _response(status, {"error": "key rejected"})

    redis, calls, _, statuses, _ = _prepare(
        monkeypatch,
        responder,
        ["auth search", "later search"],
    )

    await exa_task._scan_exa_async()

    assert [call["headers"]["x-api-key"] for call in calls] == ["key-one", "key-two"]
    assert redis.lists[exa_task.EXA_RETRY_QUEUE] == ["auth search", "later search"]
    assert "all 2 configured Exa keys were rejected" in caplog.text
    assert "check the Exa keys" in statuses[1]
