import logging

import httpx
import pytest

from app.infrastructure.scanners.providers import grepapp as grepapp_provider
from app.workers.tasks.scanners.providers import grepapp as grepapp_tasks


_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class MemoryRedis:
    def get(self, _key):
        return None


@pytest.fixture
def grepapp_env(monkeypatch):
    service = grepapp_provider.GrepAppService()
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

    monkeypatch.setattr(grepapp_tasks, "grepapp_srv", service)
    monkeypatch.setattr(grepapp_tasks, "redis_client", MemoryRedis())
    monkeypatch.setattr(grepapp_tasks, "_send_log_async", send_log)
    monkeypatch.setattr(grepapp_tasks, "_save_credentials_async", save_results)
    monkeypatch.setattr(grepapp_provider.asyncio, "sleep", no_sleep)
    return service, logs, saved, waits


def install_grepapp_handler(monkeypatch, handler):
    requests = []

    def record_request(request):
        requests.append(request)
        return handler(request)

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(record_request)
        return _HTTPX_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(grepapp_provider.httpx, "AsyncClient", make_client)
    return requests


def reply(request, status=200, payload=None, *, headers=None):
    return httpx.Response(status, json=payload, headers=headers, request=request)


def bot_token(number):
    return f"{10000000 + number}:Z{'a' * 34}"


def hit(snippet, repo="owner/repo"):
    return {"content": {"snippet": snippet}, "repo": repo}


@pytest.mark.asyncio
async def test_provider_down_is_logged_and_same_search_runs_on_next_scan(
    monkeypatch, grepapp_env, caplog
):
    _service, logs, saved, _waits = grepapp_env

    def unavailable(request):
        raise httpx.ConnectError("GrepApp cannot be reached", request=request)

    failed_requests = install_grepapp_handler(monkeypatch, unavailable)
    caplog.set_level(logging.WARNING)

    failed_result = await grepapp_tasks._scan_grepapp_async()

    assert len(failed_requests) == 4
    assert "Errors: 1" in failed_result
    assert "GrepApp search page 1" in caplog.text
    assert "ConnectError" in caplog.text
    assert "next scan" in caplog.text
    assert not saved

    recovered_requests = install_grepapp_handler(
        monkeypatch, lambda request: reply(request, payload={"hits": {"hits": []}})
    )
    recovered_result = await grepapp_tasks._scan_grepapp_async()

    assert len(recovered_requests) == 1
    assert "Errors:" not in recovered_result
    assert saved == [([], "grepapp")]
    assert any("Completed with errors" in message for message in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "attempts"),
    [(401, 1), (403, 1), (404, 1), (422, 1), (500, 4)],
)
async def test_http_errors_keep_status_and_search_context(
    monkeypatch, grepapp_env, caplog, status, attempts
):
    requests = install_grepapp_handler(
        monkeypatch,
        lambda request: reply(request, status, {"error": f"HTTP {status} error"}),
    )
    caplog.set_level(logging.WARNING)

    result = await grepapp_tasks._scan_grepapp_async()

    assert "Errors: 1" in result
    assert len(requests) == attempts
    assert "GrepApp search page 1" in caplog.text
    assert str(status) in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.asyncio
async def test_error_field_in_http_200_is_logged_as_failure(
    monkeypatch, grepapp_env, caplog
):
    install_grepapp_handler(
        monkeypatch,
        lambda request: reply(request, payload={"error": "search is not allowed"}),
    )
    caplog.set_level(logging.ERROR)

    result = await grepapp_tasks._scan_grepapp_async()

    assert "Errors: 1" in result
    assert "search is not allowed" in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_waits_then_ends_run_with_a_clear_log(
    monkeypatch, grepapp_env, caplog
):
    _service, _logs, _saved, waits = grepapp_env
    requests = install_grepapp_handler(
        monkeypatch,
        lambda request: reply(
            request, 429, {"error": "slow down"}, headers={"Retry-After": "2"}
        ),
    )
    caplog.set_level(logging.WARNING)

    result = await grepapp_tasks._scan_grepapp_async()

    assert "Errors: 1" in result
    assert len(requests) == 4
    assert waits == [2, 2, 2, 2]
    assert "rate limited" in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.asyncio
async def test_retry_after_over_sixty_seconds_stops_without_another_request(
    monkeypatch, grepapp_env, caplog
):
    _service, _logs, _saved, waits = grepapp_env
    requests = install_grepapp_handler(
        monkeypatch,
        lambda request: reply(
            request, 429, {"error": "slow down"}, headers={"Retry-After": "120"}
        ),
    )
    caplog.set_level(logging.ERROR)

    result = await grepapp_tasks._scan_grepapp_async()

    assert "Errors: 1" in result
    assert len(requests) == 1
    assert waits == []
    assert "above the 60s limit" in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.asyncio
async def test_empty_results_are_a_logged_success(monkeypatch, grepapp_env, caplog):
    _service, _logs, saved, _waits = grepapp_env
    requests = install_grepapp_handler(
        monkeypatch, lambda request: reply(request, payload={"hits": {"hits": []}})
    )
    caplog.set_level(logging.INFO)

    result = await grepapp_tasks._scan_grepapp_async()

    assert "Errors:" not in result
    assert len(requests) == 1
    assert saved == [([], "grepapp")]
    assert "returned no hits" in caplog.text


@pytest.mark.asyncio
async def test_transient_page_error_retries_and_scan_continues(
    monkeypatch, grepapp_env
):
    _service, _logs, saved, waits = grepapp_env
    pages = []

    def replies(request):
        page = int(request.url.params["page"])
        pages.append(page)
        if len(pages) == 1:
            return reply(request, 500, {"error": "temporary error"})
        if page == 1:
            token = bot_token(1)
            return reply(
                request,
                payload={"hits": {"hits": [hit(f"api.telegram.org/bot{token}")]}},
            )
        return reply(request, payload={"hits": {"hits": []}})

    install_grepapp_handler(monkeypatch, replies)

    result = await grepapp_tasks._scan_grepapp_async()

    assert "Errors:" not in result
    assert pages == [1, 1, 2]
    assert waits == [1, 1]
    assert len(saved) == 1
    assert [item["token"] for item in saved[0][0]] == [bot_token(1)]


@pytest.mark.asyncio
async def test_search_stops_at_five_pages_with_one_second_between_pages(
    monkeypatch, grepapp_env
):
    _service, _logs, _saved, waits = grepapp_env
    pages = []

    def results(request):
        page = int(request.url.params["page"])
        pages.append(page)
        hits = [hit("no token on this page")] if page < 5 else []
        return reply(request, payload={"hits": {"hits": hits}})

    install_grepapp_handler(monkeypatch, results)

    await grepapp_tasks._scan_grepapp_async()

    assert pages == [1, 2, 3, 4, 5]
    assert waits == [1, 1, 1, 1]


@pytest.mark.asyncio
async def test_search_stops_after_one_hundred_hits(monkeypatch, grepapp_env):
    _service, _logs, saved, _waits = grepapp_env
    hits = [hit(f"api.telegram.org/bot{bot_token(i)}") for i in range(101)]
    requests = install_grepapp_handler(
        monkeypatch, lambda request: reply(request, payload={"hits": {"hits": hits}})
    )

    result = await grepapp_tasks._scan_grepapp_async()

    assert "Saved 100 new credentials" in result
    assert len(requests) == 1
    assert len(saved[0][0]) == 100
