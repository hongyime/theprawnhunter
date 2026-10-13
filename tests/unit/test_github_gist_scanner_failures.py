import json
import logging

import httpx
import pytest

from app.core.config import settings
from app.infrastructure.scanners.providers import github_gist as gist_provider
from app.workers.tasks.scanners.providers import github_gist as gist_tasks
from app.workers.tasks import flow_tasks


_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.eval_calls = []
        self.rotation = 0

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **_kwargs):
        self.values[key] = str(value)
        return True

    def delete(self, *keys):
        removed = 0
        for key in keys:
            removed += int(self.values.pop(key, None) is not None)
        return removed

    def incr(self, _key):
        self.rotation += 1
        return self.rotation

    def eval(self, script, key_count, *args):
        self.eval_calls.append((script, key_count, args))
        return 1


@pytest.fixture
def gist_env(monkeypatch):
    redis = MemoryRedis()
    service = gist_provider.GithubGistService()
    logs = []
    saved = []
    waits = []

    async def send_log(message):
        logs.append(message)

    async def save_results(results, source):
        saved.append((results, source))
        return len(results)

    async def no_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(settings, "GITHUB_TOKEN", "test-github-token", raising=False)
    monkeypatch.setattr(settings, "GITHUB_TOKENS", "", raising=False)
    service.token = "test-github-token"
    monkeypatch.setattr(flow_tasks, "redis_client", redis)
    monkeypatch.setattr(gist_tasks, "gist_srv", service)
    monkeypatch.setattr(gist_tasks, "redis_client", redis)
    monkeypatch.setattr(gist_tasks, "_send_log_async", send_log)
    monkeypatch.setattr(gist_tasks, "_save_credentials_async", save_results)
    monkeypatch.setattr(gist_provider.asyncio, "sleep", no_sleep)
    return service, redis, logs, saved, waits


def install_github_handler(monkeypatch, handler):
    requests = []

    def record_request(request):
        requests.append(request)
        return handler(request)

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(record_request)
        return _HTTPX_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(gist_provider.httpx, "AsyncClient", make_client)
    return requests


def json_reply(request, status=200, payload=None, headers=None):
    return httpx.Response(status, json=payload, headers=headers, request=request)


def text_reply(request, status=200, text="", headers=None):
    return httpx.Response(status, text=text, headers=headers, request=request)


def gist(gist_id, filename, raw_url):
    return {
        "id": gist_id,
        "files": {filename: {"raw_url": raw_url}},
    }


def bot_token(number):
    return f"{10000000 + number}:Z{'a' * 34}"


@pytest.mark.asyncio
async def test_provider_down_still_checks_saved_file_and_logs_next_step(
    monkeypatch, gist_env, caplog
):
    _service, redis, _logs, saved, _waits = gist_env
    raw_url = "https://gist.githubusercontent.com/old/1/raw/old.txt"
    redis.set(
        gist_tasks.GIST_RETRY_FILES_KEY,
        json.dumps([{"gist_id": "old-gist", "file": "old.txt", "raw_url": raw_url}]),
    )

    def handler(request):
        if request.url.host == "api.github.com":
            raise httpx.ConnectError("GitHub is unreachable", request=request)
        return text_reply(request, text=bot_token(1))

    requests = install_github_handler(monkeypatch, handler)
    caplog.set_level(logging.ERROR)

    result = await gist_tasks._scan_gist_async()

    assert len(requests) == 2
    assert saved[0][1] == "gist"
    assert saved[0][0][0]["token"] == bot_token(1)
    assert redis.get(gist_tasks.GIST_RETRY_FILES_KEY) is None
    assert "completed with errors" in result
    assert "Public Gist page 1 failed" in caplog.text
    assert "GitHub is unreachable" in caplog.text
    assert "scheduled scan will request this page again" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"message": "Bad credentials"}, "HTTP 401"),
        (403, {"message": "Access denied"}, "HTTP 403"),
        (404, {"message": "Not found"}, "HTTP 404"),
        (422, {"message": "Invalid request"}, "HTTP 422"),
        (500, {"message": "GitHub server failed"}, "HTTP 500"),
        (200, {"message": "Gist search error"}, "error inside HTTP 200"),
    ],
)
async def test_public_gist_errors_are_logged_and_return_with_error(
    monkeypatch, gist_env, caplog, status, payload, expected
):
    _service, _redis, _logs, saved, _waits = gist_env
    requests = install_github_handler(
        monkeypatch,
        lambda request: json_reply(request, status, payload),
    )
    caplog.set_level(logging.ERROR)

    result = await gist_tasks._scan_gist_async()

    assert len(requests) == 1
    assert expected in result
    assert expected in caplog.text
    assert saved == [([], "gist")]


@pytest.mark.asyncio
async def test_secondary_rate_limit_waits_once_and_keeps_old_file_for_next_scan(
    monkeypatch, gist_env, caplog
):
    _service, redis, _logs, _saved, waits = gist_env
    raw_url = "https://gist.githubusercontent.com/old/1/raw/old.txt"
    redis.set(
        gist_tasks.GIST_RETRY_FILES_KEY,
        json.dumps([{"gist_id": "old-gist", "file": "old.txt", "raw_url": raw_url}]),
    )
    requests = install_github_handler(
        monkeypatch,
        lambda request: json_reply(
            request,
            403,
            {"message": "You have exceeded a secondary rate limit"},
            {"Retry-After": "2"},
        ),
    )
    caplog.set_level(logging.ERROR)

    result = await gist_tasks._scan_gist_async()

    assert len(requests) == 2
    assert waits == [2.0]
    assert redis.get(gist_tasks.GIST_RETRY_FILES_KEY) is not None
    assert any(
        args[0] == gist_provider.GITHUB_SEARCH_PAUSE_KEY and args[-1] == 2000
        for _script, key_count, args in redis.eval_calls
        if key_count == 1
    )
    assert "rate limited" in result
    assert "secondary rate limit" in caplog.text
    assert "saved for the next scan" in caplog.text


@pytest.mark.asyncio
async def test_long_rate_limit_wait_stops_without_another_request(
    monkeypatch, gist_env, caplog
):
    _service, _redis, _logs, _saved, waits = gist_env
    requests = install_github_handler(
        monkeypatch,
        lambda request: json_reply(
            request,
            429,
            {"message": "Slow down"},
            {"Retry-After": "600"},
        ),
    )
    caplog.set_level(logging.ERROR)

    result = await gist_tasks._scan_gist_async()

    assert len(requests) == 1
    assert waits == []
    assert "600s" in result
    assert "next scheduled scan will try it again" in caplog.text


@pytest.mark.asyncio
async def test_empty_gist_list_is_logged_as_empty(monkeypatch, gist_env, caplog):
    _service, _redis, _logs, saved, _waits = gist_env
    requests = install_github_handler(monkeypatch, lambda request: json_reply(request, payload=[]))
    caplog.set_level(logging.INFO)

    result = await gist_tasks._scan_gist_async()

    assert len(requests) == 1
    assert requests[0].url.params["per_page"] == "100"
    assert requests[0].url.params["page"] == "1"
    assert "no Gists" in caplog.text
    assert "Errors:" not in result
    assert saved == [([], "gist")]


@pytest.mark.asyncio
async def test_failed_file_does_not_stop_other_gists_and_is_retried_next_scan(
    monkeypatch, gist_env, caplog
):
    _service, redis, _logs, saved, waits = gist_env
    raw_a = "https://gist.githubusercontent.com/a/1/raw/a.txt"
    raw_b = "https://gist.githubusercontent.com/b/1/raw/b.txt"
    api_calls = 0
    raw_a_calls = 0

    def handler(request):
        nonlocal api_calls, raw_a_calls
        if request.url.host == "api.github.com":
            api_calls += 1
            if api_calls == 1:
                return json_reply(
                    request,
                    payload=[gist("gist-a", "a.txt", raw_a), gist("gist-b", "b.txt", raw_b)],
                )
            return json_reply(request, payload=[])
        if str(request.url) == raw_a:
            raw_a_calls += 1
            if raw_a_calls == 1:
                return text_reply(request, 500, "Raw file failed")
            return text_reply(request, text=bot_token(2))
        return text_reply(request, text=bot_token(1))

    requests = install_github_handler(monkeypatch, handler)
    caplog.set_level(logging.ERROR)

    first_result = await gist_tasks._scan_gist_async()

    pending = json.loads(redis.get(gist_tasks.GIST_RETRY_FILES_KEY))
    assert pending == [{"gist_id": "gist-a", "file": "a.txt", "raw_url": raw_a}]
    assert saved[0][0][0]["token"] == bot_token(1)
    assert "1 Gist files remain saved" in first_result
    assert "Gist gist-a file a.txt failed" in caplog.text
    assert "HTTP 500: Raw file failed" in caplog.text
    assert "Saved for the next scan" in caplog.text
    assert waits == [1.0]

    second_result = await gist_tasks._scan_gist_async()

    assert raw_a_calls == 2
    assert redis.get(gist_tasks.GIST_RETRY_FILES_KEY) is None
    assert saved[1][0][0]["token"] == bot_token(2)
    assert "Errors:" not in second_result
    assert len(requests) == 5


@pytest.mark.asyncio
async def test_successful_fetch_stays_saved_when_result_saving_fails(
    monkeypatch, gist_env, caplog
):
    _service, redis, _logs, saved, _waits = gist_env
    raw_url = "https://gist.githubusercontent.com/save/1/raw/save.txt"
    api_calls = 0
    save_calls = 0

    def handler(request):
        nonlocal api_calls
        if request.url.host == "api.github.com":
            api_calls += 1
            if api_calls == 1:
                return json_reply(request, payload=[gist("save-gist", "save.txt", raw_url)])
            return json_reply(request, payload=[])
        return text_reply(request, text=bot_token(3))

    async def save_results(results, source):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            raise RuntimeError("result queue is unavailable")
        saved.append((results, source))
        return len(results)

    install_github_handler(monkeypatch, handler)
    monkeypatch.setattr(gist_tasks, "_save_credentials_async", save_results)
    caplog.set_level(logging.ERROR)

    with pytest.raises(RuntimeError, match="retry this scan"):
        await gist_tasks._scan_gist_async()

    pending = json.loads(redis.get(gist_tasks.GIST_RETRY_FILES_KEY))
    assert pending == [{"gist_id": "save-gist", "file": "save.txt", "raw_url": raw_url}]
    assert "All checked files remain saved" in caplog.text

    result = await gist_tasks._scan_gist_async()

    assert redis.get(gist_tasks.GIST_RETRY_FILES_KEY) is None
    assert saved[0][0][0]["token"] == bot_token(3)
    assert "Errors:" not in result


@pytest.mark.asyncio
async def test_raw_file_rate_limit_waits_retries_and_shares_the_pause(
    monkeypatch, gist_env
):
    _service, redis, _logs, saved, waits = gist_env
    raw_a = "https://gist.githubusercontent.com/a/1/raw/a.txt"
    raw_b = "https://gist.githubusercontent.com/b/1/raw/b.txt"
    raw_a_calls = 0

    def handler(request):
        nonlocal raw_a_calls
        if request.url.host == "api.github.com":
            return json_reply(
                request,
                payload=[gist("gist-a", "a.txt", raw_a), gist("gist-b", "b.txt", raw_b)],
            )
        if str(request.url) == raw_a:
            raw_a_calls += 1
            if raw_a_calls == 1:
                return text_reply(request, 429, "Slow down", {"Retry-After": "1"})
            return text_reply(request, text=bot_token(1))
        return text_reply(request, text=bot_token(2))

    requests = install_github_handler(monkeypatch, handler)

    result = await gist_tasks._scan_gist_async()

    assert raw_a_calls == 2
    assert len(requests) == 4
    assert waits == [1.0, 1.0]
    assert len(redis.eval_calls) == 1
    assert redis.eval_calls[0][2][0] == gist_provider.GITHUB_SEARCH_PAUSE_KEY
    assert [item["token"] for item in saved[0][0]] == [bot_token(1), bot_token(2)]
    assert "Errors:" not in result


@pytest.mark.asyncio
async def test_missing_token_warns_and_keeps_saved_retries(monkeypatch, gist_env, caplog):
    service, redis, _logs, saved, _waits = gist_env
    raw_url = "https://gist.githubusercontent.com/old/1/raw/old.txt"
    redis.set(
        gist_tasks.GIST_RETRY_FILES_KEY,
        json.dumps([{"gist_id": "old-gist", "file": "old.txt", "raw_url": raw_url}]),
    )
    service.token = ""
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "", raising=False)
    requests = install_github_handler(
        monkeypatch,
        lambda request: pytest.fail("GitHub request started without a login token"),
    )
    caplog.set_level(logging.WARNING)

    result = await gist_tasks._scan_gist_async()

    assert requests == []
    assert json.loads(redis.get(gist_tasks.GIST_RETRY_FILES_KEY))[0]["raw_url"] == raw_url
    assert "login token is missing or empty" in result
    assert "login token is missing or empty" in caplog.text
    assert saved == [([], "gist")]


def test_gist_uses_the_github_search_token_rotation(monkeypatch, gist_env):
    service, _redis, _logs, _saved, _waits = gist_env
    monkeypatch.setattr(settings, "GITHUB_TOKENS", "token-a,token-b", raising=False)

    assert service._get_token() == "token-b"
    assert service._get_token() == "token-a"
