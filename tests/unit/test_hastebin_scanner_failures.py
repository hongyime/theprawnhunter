import logging

import httpx
import pytest

from app.infrastructure.scanners.providers import exa as exa_provider
from app.infrastructure.scanners.providers import hastebin as hastebin_provider
from app.workers.tasks import flow_tasks
from app.workers.tasks.scanners.providers import hastebin as hastebin_task


FIRST_QUERY = 'site:hastebin.com "api.telegram.org/bot"'
SECOND_QUERY = 'site:hastebin.com "TELEGRAM_BOT_TOKEN"'
TOKEN_ONE = f"12345678:{'Z' * 35}"
TOKEN_TWO = f"23456789:{'Y' * 35}"


class _MemoryRedis:
    def __init__(self):
        self.values = {}
        self.sets = {}

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return int(key in self.values)

    def setex(self, key, _ttl, value):
        self.values[key] = value
        return True

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def sadd(self, key, value):
        before = len(self.sets.setdefault(key, set()))
        self.sets[key].add(value)
        return int(len(self.sets[key]) > before)

    def srem(self, key, value):
        values = self.sets.setdefault(key, set())
        existed = value in values
        values.discard(value)
        return int(existed)


class _Context:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_args):
        return False


class _ExaClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def post(self, url, *, headers, json):
        query = json["query"]
        self.calls.append(query)
        return self.responder(query, self.calls.count(query))


class _RawClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        return self.responder(url, self.calls.count(url))


def _response(
    status=200,
    *,
    payload=None,
    text=None,
    headers=None,
    url="https://api.exa.ai/search",
):
    request = httpx.Request("GET", url)
    if payload is not None:
        return httpx.Response(status, json=payload, headers=headers or {}, request=request)
    if text is not None:
        return httpx.Response(status, text=text, headers=headers or {}, request=request)
    return httpx.Response(status, headers=headers or {}, request=request)


def _paste_result(url, token):
    return {"text": f"bot{token}", "url": url}


def _prepare(monkeypatch, exa_responder, raw_responder):
    redis = _MemoryRedis()
    exa_client = _ExaClient(exa_responder)
    raw_client = _RawClient(raw_responder)
    saved = []
    statuses = []
    sleeps = []

    monkeypatch.setattr(exa_provider.settings, "EXA_API_KEY", "test-exa-key", raising=False)
    monkeypatch.setattr(exa_provider.settings, "EXA_API_KEY_2", "", raising=False)
    monkeypatch.setattr(exa_provider.settings, "EXA_API_KEY_3", "", raising=False)
    monkeypatch.setattr(
        exa_provider,
        "get_async_http_client",
        lambda **_kwargs: _Context(exa_client),
    )
    monkeypatch.setattr(
        hastebin_provider.httpx,
        "AsyncClient",
        lambda **_kwargs: _Context(raw_client),
    )
    monkeypatch.setattr(flow_tasks, "redis_client", redis)
    monkeypatch.setattr(hastebin_task, "redis_client", redis)

    async def save_results(results, source_name):
        saved.extend((source_name, item) for item in results)
        return len(results)

    async def send_status(message):
        statuses.append(message)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(hastebin_task, "_save_credentials_async", save_results)
    monkeypatch.setattr(hastebin_task, "_send_log_async", send_status)
    monkeypatch.setattr(hastebin_provider.asyncio, "sleep", sleep)
    return redis, exa_client, raw_client, saved, statuses, sleeps


def _exa_with_pastes(first_results):
    def respond(query, _attempt):
        results = first_results if query == FIRST_QUERY else []
        return _response(payload={"results": results})

    return respond


@pytest.mark.asyncio
async def test_exa_outage_is_logged_and_the_other_search_still_runs(monkeypatch, caplog):
    paste_id = "query-two-paste"

    def respond(query, _attempt):
        if query == FIRST_QUERY:
            return _response(503, payload={"error": "search service down"})
        return _response(
            payload={
                "results": [
                    _paste_result(f"https://hastebin.com/{paste_id}", TOKEN_ONE)
                ]
            }
        )

    redis, exa, raw, saved, _, sleeps = _prepare(
        monkeypatch,
        respond,
        lambda url, _attempt: _response(200, text=TOKEN_ONE, url=url),
    )
    caplog.set_level(logging.INFO, logger="scanners")

    result = await hastebin_task._scan_hastebin_async()

    assert exa.calls == [FIRST_QUERY] * exa_provider.MAX_ATTEMPTS + [SECOND_QUERY]
    assert raw.calls == [f"https://hastebin.com/raw/{paste_id}"]
    assert len(saved) == 1 and saved[0][1]["meta"]["paste_id"] == paste_id
    assert paste_id not in redis.sets[hastebin_provider.HastebinService.PENDING_KEY]
    assert f"hastebin:seen:{paste_id}" in redis.values
    assert "Search" in caplog.text and FIRST_QUERY in caplog.text
    assert "continuing with the other search" in caplog.text
    assert 2 in sleeps and 4 in sleeps and 8 in sleeps
    assert result.endswith("Saved 1 new credentials.")


@pytest.mark.parametrize(
    ("status", "expected_fetches", "message"),
    [
        (401, 1, "HTTP 401"),
        (403, 1, "HTTP 403"),
        (404, 1, "missing or deleted"),
        (410, 1, "missing or deleted"),
        (422, 1, "HTTP 422"),
        (500, 3, "HTTP 500"),
    ],
)
@pytest.mark.asyncio
async def test_http_errors_are_logged_and_paste_stays_pending(
    monkeypatch, caplog, status, expected_fetches, message
):
    paste_id = f"http-{status}"
    redis, _, raw, _, _, _ = _prepare(
        monkeypatch,
        _exa_with_pastes(
            [_paste_result(f"https://hastebin.com/{paste_id}", TOKEN_ONE)]
        ),
        lambda url, _attempt: _response(status, text="error", url=url),
    )
    caplog.set_level(logging.INFO, logger="scanners")

    await hastebin_task._scan_hastebin_async()

    assert len(raw.calls) == expected_fetches
    assert paste_id in redis.sets[hastebin_provider.HastebinService.PENDING_KEY]
    assert f"hastebin:seen:{paste_id}" not in redis.values
    assert paste_id in caplog.text and message in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.parametrize(
    ("text", "headers", "message"),
    [
        ("", {}, "empty HTTP 200 reply"),
        ("Loading paste", {}, "loading reply"),
        ("Document not found", {}, "missing, deleted, or loading reply"),
        (
            '{"error":"upstream failed"}',
            {"content-type": "application/json"},
            "error in HTTP 200 reply",
        ),
    ],
)
@pytest.mark.asyncio
async def test_error_loading_or_empty_200_reply_stays_pending(
    monkeypatch, caplog, text, headers, message
):
    paste_id = "bad-200-reply"
    redis, _, _, _, _, _ = _prepare(
        monkeypatch,
        _exa_with_pastes(
            [_paste_result(f"https://hastebin.com/{paste_id}", TOKEN_ONE)]
        ),
        lambda url, _attempt: _response(200, text=text, headers=headers, url=url),
    )
    caplog.set_level(logging.INFO, logger="scanners")

    await hastebin_task._scan_hastebin_async()

    assert paste_id in redis.sets[hastebin_provider.HastebinService.PENDING_KEY]
    assert f"hastebin:seen:{paste_id}" not in redis.values
    assert paste_id in caplog.text and message in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.asyncio
async def test_raw_service_down_keeps_paste_and_reads_the_next_one(monkeypatch, caplog):
    first_id = "paste-down"
    second_id = "paste-up"

    def raw_reply(url, _attempt):
        if first_id in url:
            raise httpx.ConnectError("Hastebin cannot be reached")
        return _response(200, text=TOKEN_TWO, url=url)

    redis, _, raw, saved, _, _ = _prepare(
        monkeypatch,
        _exa_with_pastes(
            [
                _paste_result(f"https://hastebin.com/{first_id}", TOKEN_ONE),
                _paste_result(f"https://hastebin.com/{second_id}", TOKEN_TWO),
            ]
        ),
        raw_reply,
    )
    caplog.set_level(logging.INFO, logger="scanners")

    result = await hastebin_task._scan_hastebin_async()

    assert raw.calls.count(f"https://hastebin.com/raw/{first_id}") == 3
    assert raw.calls.count(f"https://hastebin.com/raw/{second_id}") == 1
    assert redis.sets[hastebin_provider.HastebinService.PENDING_KEY] == {first_id}
    assert f"hastebin:seen:{first_id}" not in redis.values
    assert f"hastebin:seen:{second_id}" in redis.values
    assert len(saved) == 1 and saved[0][1]["meta"]["paste_id"] == second_id
    assert "connection failed after 3 requests" in caplog.text
    assert "remains pending for the next scan" in caplog.text
    assert result.endswith("Saved 1 new credentials.")


@pytest.mark.asyncio
async def test_bad_search_url_is_logged_and_later_paste_is_read(monkeypatch, caplog):
    paste_id = "valid-paste"
    redis, _, raw, saved, _, _ = _prepare(
        monkeypatch,
        _exa_with_pastes(
            [
                _paste_result("https://hastebin.com/[broken", TOKEN_ONE),
                _paste_result(f"https://hastebin.com/{paste_id}", TOKEN_TWO),
            ]
        ),
        lambda url, _attempt: _response(200, text=TOKEN_TWO, url=url),
    )
    caplog.set_level(logging.INFO, logger="scanners")

    await hastebin_task._scan_hastebin_async()

    assert raw.calls == [f"https://hastebin.com/raw/{paste_id}"]
    assert len(saved) == 1 and saved[0][1]["meta"]["paste_id"] == paste_id
    assert paste_id not in redis.sets[hastebin_provider.HastebinService.PENDING_KEY]
    assert "invalid Hastebin URL" in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_waits_then_retries_the_same_paste(monkeypatch, caplog):
    paste_id = "rate-limited"

    def raw_reply(url, attempt):
        if attempt == 1:
            return _response(429, text="slow down", headers={"Retry-After": "2"}, url=url)
        return _response(200, text=TOKEN_ONE, url=url)

    redis, _, raw, saved, _, sleeps = _prepare(
        monkeypatch,
        _exa_with_pastes(
            [_paste_result(f"https://hastebin.com/{paste_id}", TOKEN_ONE)]
        ),
        raw_reply,
    )
    caplog.set_level(logging.INFO, logger="scanners")

    await hastebin_task._scan_hastebin_async()

    assert raw.calls.count(f"https://hastebin.com/raw/{paste_id}") == 2
    assert 2 in sleeps
    assert "HTTP 429" in caplog.text and "Waiting 2s, then retrying" in caplog.text
    assert len(saved) == 1
    assert f"hastebin:seen:{paste_id}" in redis.values
    assert paste_id not in redis.sets[hastebin_provider.HastebinService.PENDING_KEY]


@pytest.mark.asyncio
async def test_long_rate_limit_stops_raw_fetches_and_keeps_later_pastes(monkeypatch, caplog):
    first_id = "a-rate-limited"
    second_id = "z-later-paste"
    redis, _, raw, _, _, sleeps = _prepare(
        monkeypatch,
        _exa_with_pastes(
            [
                _paste_result(f"https://hastebin.com/{first_id}", TOKEN_ONE),
                _paste_result(f"https://hastebin.com/{second_id}", TOKEN_TWO),
            ]
        ),
        lambda url, _attempt: _response(
            429, text="slow down", headers={"Retry-After": "90"}, url=url
        ),
    )
    caplog.set_level(logging.INFO, logger="scanners")

    await hastebin_task._scan_hastebin_async()

    assert raw.calls == [f"https://hastebin.com/raw/{first_id}"]
    assert sleeps == []
    assert redis.sets[hastebin_provider.HastebinService.PENDING_KEY] == {
        first_id,
        second_id,
    }
    assert "stopping raw fetches for this run" in caplog.text.casefold()
    assert "next scan" in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_message_in_http_200_stops_raw_fetches(monkeypatch, caplog):
    first_id = "a-too-fast"
    second_id = "z-later-paste"
    redis, _, raw, _, _, sleeps = _prepare(
        monkeypatch,
        _exa_with_pastes(
            [
                _paste_result(f"https://hastebin.com/{first_id}", TOKEN_ONE),
                _paste_result(f"https://hastebin.com/{second_id}", TOKEN_TWO),
            ]
        ),
        lambda url, _attempt: _response(
            200, text="Too many requests, please slow down", url=url
        ),
    )
    caplog.set_level(logging.INFO, logger="scanners")

    await hastebin_task._scan_hastebin_async()

    assert raw.calls == [f"https://hastebin.com/raw/{first_id}"]
    assert sleeps == []
    assert redis.sets[hastebin_provider.HastebinService.PENDING_KEY] == {
        first_id,
        second_id,
    }
    assert "rate-limit error in HTTP 200 reply" in caplog.text
    assert "stopping raw fetches for this run" in caplog.text.casefold()


@pytest.mark.asyncio
async def test_empty_search_results_are_logged_without_raw_fetches(monkeypatch, caplog):
    redis, exa, raw, _, _, _ = _prepare(
        monkeypatch,
        lambda _query, _attempt: _response(payload={"results": []}),
        lambda *_: _response(404),
    )
    caplog.set_level(logging.INFO, logger="scanners")

    await hastebin_task._scan_hastebin_async()

    assert exa.calls == [FIRST_QUERY, SECOND_QUERY]
    assert raw.calls == []
    assert caplog.text.count("returned no token matches") == 2
    assert redis.sets.get(hastebin_provider.HastebinService.PENDING_KEY, set()) == set()


@pytest.mark.asyncio
async def test_missing_exa_key_is_logged_and_returns_cleanly(monkeypatch, caplog):
    redis, exa, raw, _, _, _ = _prepare(
        monkeypatch,
        lambda *_: _response(payload={"results": []}),
        lambda *_: _response(404),
    )
    monkeypatch.setattr(exa_provider.settings, "EXA_API_KEY", "", raising=False)
    caplog.set_level(logging.INFO, logger="scanners")

    result = await hastebin_task._scan_hastebin_async()

    assert exa.calls == []
    assert raw.calls == []
    assert "no non-empty Exa API key" in caplog.text
    assert "Search" in caplog.text and "Remaining searches will run next scan" in caplog.text
    assert result.endswith("Saved 0 new credentials.")
    assert redis.sets.get(hastebin_provider.HastebinService.PENDING_KEY, set()) == set()


@pytest.mark.asyncio
async def test_save_failure_keeps_token_paste_pending(monkeypatch, caplog):
    paste_id = "save-retry"
    redis, _, _, _, _, _ = _prepare(
        monkeypatch,
        _exa_with_pastes(
            [_paste_result(f"https://hastebin.com/{paste_id}", TOKEN_ONE)]
        ),
        lambda url, _attempt: _response(200, text=TOKEN_ONE, url=url),
    )

    async def fail_to_save(_results, _source_name):
        raise RuntimeError("validation queue is down")

    monkeypatch.setattr(hastebin_task, "_save_credentials_async", fail_to_save)
    caplog.set_level(logging.INFO, logger="scanner.tasks")

    result = await hastebin_task._scan_hastebin_async()

    assert paste_id in redis.sets[hastebin_provider.HastebinService.PENDING_KEY]
    assert f"hastebin:seen:{paste_id}" not in redis.values
    assert f"paste(s) {paste_id}" in caplog.text
    assert "remain pending for the next scan" in caplog.text
    assert "Errors: 1" in result
