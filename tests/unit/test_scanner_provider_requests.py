import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest


class _GithubRedis:
    def __init__(self, retry_queries=(), waits=()):
        self.retry_queries = set(retry_queries)
        self.waits = list(waits)
        self.eval_calls = []
        self.pause_values = []

    def smembers(self, _key):
        return set(self.retry_queries)

    @staticmethod
    def get(_key):
        return None

    def sadd(self, _key, value):
        self.retry_queries.add(value)
        return 1

    def srem(self, _key, value):
        self.retry_queries.discard(value)
        return 1

    def eval(self, script, key_count, *args):
        self.eval_calls.append((script, key_count, args))
        if key_count == 1:
            self.pause_values.append(args[-1])
            return 1
        if self.waits:
            return self.waits.pop(0)
        return 0


class _GithubApiContext:
    def __init__(self, response_for):
        self.response_for = response_for

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **kwargs):
        return await self.response_for(url, **kwargs)


def _github_response(status_code=200, payload=None, headers=None, url="https://api.github.com/search/code"):
    return httpx.Response(
        status_code,
        json=payload or {},
        headers=headers or {},
        request=httpx.Request("GET", url),
    )


def _install_github_redis(monkeypatch, redis_client, github_task=None):
    from app.workers.tasks import flow_tasks

    monkeypatch.setattr(flow_tasks, "redis_client", redis_client)
    if github_task is not None:
        monkeypatch.setattr(github_task, "redis_client", redis_client)


def _make_github_service(provider, monkeypatch):
    monkeypatch.setattr(provider.settings, "GITHUB_TOKENS", "", raising=False)
    service = provider.GithubService()
    service.token = "test-token"
    return service


def _install_github_api(monkeypatch, provider, response_for):
    monkeypatch.setattr(
        provider,
        "get_async_http_client",
        lambda **_kwargs: _GithubApiContext(response_for),
    )


@pytest.mark.asyncio
async def test_netlas_scan_calls_search_with_only_the_query(monkeypatch):
    from app.workers.tasks.scanners.providers import netlas as netlas_task

    search_calls = []

    class RedisClient:
        @staticmethod
        def get(_key):
            return None

    class NetlasService:
        @staticmethod
        async def get_usage_summary():
            return {"account_1": {"used": 0, "limit": 45, "remaining": 1}}

        @staticmethod
        async def search(query):
            search_calls.append(query)
            return []

    async def no_op(*_args, **_kwargs):
        return None

    async def save_no_op(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(netlas_task, "redis_client", RedisClient())
    monkeypatch.setattr(netlas_task, "netlas_srv", NetlasService())
    monkeypatch.setattr(netlas_task, "_send_log_async", no_op)
    monkeypatch.setattr(netlas_task, "_save_credentials_async", save_no_op)
    monkeypatch.setattr(netlas_task.asyncio, "sleep", no_op)

    await netlas_task._scan_netlas_async("http.body:telegram")

    assert search_calls == ["http.body:telegram"]


@pytest.mark.asyncio
async def test_urlscan_request_uses_supported_query_and_page_size(monkeypatch):
    from app.infrastructure.scanners.providers import urlscan as urlscan_provider

    request = {}

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"results": []}

    class Client:
        @staticmethod
        async def get(_url, *, headers, params):
            request.update(headers=headers, params=params)
            return Response()

    class ClientContext:
        async def __aenter__(self):
            return Client()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        urlscan_provider,
        "get_async_http_client",
        lambda **_kwargs: ClientContext(),
    )
    service = urlscan_provider.UrlScanService()
    service.api_key = "test-key"

    await service.search("bot_token")

    assert request["params"] == {"q": "page.url:*bot_token*", "size": 100}


def test_github_pushed_cutoff_rolls_forward(monkeypatch):
    from app.infrastructure.scanners.providers.github import GithubService

    monkeypatch.setenv("GITHUB_PUSHED_WITHIN_DAYS", "14")
    service = GithubService()
    first_now = datetime(2026, 10, 1, tzinfo=UTC)
    second_now = first_now + timedelta(days=1)

    first_cutoff = service.recent_push_cutoff(first_now)
    second_cutoff = service.recent_push_cutoff(second_now)

    assert first_cutoff == first_now - timedelta(days=14)
    assert second_cutoff == first_cutoff + timedelta(days=1)


@pytest.mark.asyncio
async def test_github_search_skips_old_repositories(monkeypatch):
    from app.infrastructure.scanners.providers import github as github_provider

    redis_client = _GithubRedis()
    _install_github_redis(monkeypatch, redis_client)
    now = datetime.now(UTC)
    recent_push = (now - timedelta(days=2)).isoformat()
    old_push = (now - timedelta(days=40)).isoformat()
    raw_urls = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        @staticmethod
        def raise_for_status():
            return None

        def json(self):
            return self.payload

    class ApiClient:
        @staticmethod
        async def get(url, *, headers, params=None):
            if url.endswith("/search/code"):
                return Response({"items": [
                    {
                        "html_url": "https://github.com/new/bot/blob/main/.env",
                        "path": ".env",
                        "sha": "fresh-sha",
                        "repository": {"full_name": "new/bot"},
                    },
                    {
                        "html_url": "https://github.com/old/bot/blob/main/.env",
                        "path": ".env",
                        "sha": "old-sha",
                        "repository": {"full_name": "old/bot", "pushed_at": old_push},
                    },
                ]})
            assert url.endswith("/repos/new/bot")
            return Response({"pushed_at": recent_push})

    class ApiClientContext:
        async def __aenter__(self):
            return ApiClient()

        async def __aexit__(self, *_args):
            return None

    class RawResponse:
        status_code = 200
        headers = {}
        text = ""

        @staticmethod
        def raise_for_status():
            return None

    class RawClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers):
            raw_urls.append(url)
            return RawResponse()

    monkeypatch.setattr(
        github_provider,
        "get_async_http_client",
        lambda **_kwargs: ApiClientContext(),
    )
    monkeypatch.setattr(github_provider.httpx, "AsyncClient", RawClient)
    service = _make_github_service(github_provider, monkeypatch)

    await service.search(
        'filename:.env "TELEGRAM_BOT_TOKEN"',
        pushed_since=service.recent_push_cutoff(now),
    )

    assert raw_urls == ["https://raw.githubusercontent.com/new/bot/main/.env"]


@pytest.mark.asyncio
async def test_github_search_reports_provider_down(monkeypatch):
    from app.infrastructure.scanners.providers import github as github_provider

    _install_github_redis(monkeypatch, _GithubRedis())

    async def response_for(url, **_kwargs):
        request = httpx.Request("GET", url)
        raise httpx.ConnectError("connection refused", request=request)

    _install_github_api(monkeypatch, github_provider, response_for)
    service = _make_github_service(github_provider, monkeypatch)

    with pytest.raises(github_provider.GithubSearchError, match="could not reach GitHub"):
        await service.search('filename:.env "TELEGRAM_BOT_TOKEN"')


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 404, 422, 500, 503])
async def test_github_search_reports_provider_http_errors(monkeypatch, status_code):
    from app.infrastructure.scanners.providers import github as github_provider

    _install_github_redis(monkeypatch, _GithubRedis())

    async def response_for(url, **_kwargs):
        return _github_response(
            status_code,
            {"message": "provider rejected this request"},
            url=url,
        )

    _install_github_api(monkeypatch, github_provider, response_for)
    service = _make_github_service(github_provider, monkeypatch)

    with pytest.raises(
        github_provider.GithubSearchError,
        match=f"returned HTTP {status_code}.*provider rejected this request",
    ):
        await service.search('filename:.env "TELEGRAM_BOT_TOKEN"')


@pytest.mark.asyncio
@pytest.mark.parametrize("rate_reply", ["too_many", "secondary", "primary"])
async def test_github_search_reports_rate_limit_and_wait(monkeypatch, rate_reply):
    from app.infrastructure.scanners.providers import github as github_provider

    redis_client = _GithubRedis()
    _install_github_redis(monkeypatch, redis_client)

    async def response_for(url, **_kwargs):
        if rate_reply == "too_many":
            return _github_response(
                429,
                {"message": "Too many requests"},
                {"Retry-After": "4"},
                url,
            )
        if rate_reply == "secondary":
            return _github_response(
                403,
                {"message": "You have exceeded a secondary rate limit."},
                url=url,
            )
        return _github_response(
            403,
            {"message": "API rate limit exceeded"},
            {
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(time.time() + 20),
            },
            url,
        )

    _install_github_api(monkeypatch, github_provider, response_for)
    service = _make_github_service(github_provider, monkeypatch)

    with pytest.raises(github_provider.GithubRateLimitError) as raised:
        await service.search('filename:.env "TELEGRAM_BOT_TOKEN"')

    if rate_reply == "too_many":
        assert raised.value.wait_seconds == 4
    elif rate_reply == "secondary":
        assert raised.value.wait_seconds == 60
    else:
        assert 19 <= raised.value.wait_seconds <= 20
    assert redis_client.pause_values


@pytest.mark.asyncio
async def test_github_search_returns_empty_results_as_success(monkeypatch):
    from app.infrastructure.scanners.providers import github as github_provider

    _install_github_redis(monkeypatch, _GithubRedis())
    request = {}

    async def response_for(url, *, headers, params):
        request.update(url=url, headers=headers, params=params)
        return _github_response(payload={"items": [], "incomplete_results": False}, url=url)

    _install_github_api(monkeypatch, github_provider, response_for)
    service = _make_github_service(github_provider, monkeypatch)

    assert await service.search('filename:.env "TELEGRAM_BOT_TOKEN"') == []
    assert request["params"]["per_page"] == 100


@pytest.mark.asyncio
async def test_github_raw_fetch_stops_queued_requests_after_rate_limit(monkeypatch):
    from app.infrastructure.scanners.providers import github as github_provider

    redis_client = _GithubRedis()
    _install_github_redis(monkeypatch, redis_client)
    raw_requests = []

    async def response_for(url, **_kwargs):
        items = [
            {
                "html_url": f"https://github.com/example/bot/blob/main/.env.{index}",
                "path": f".env.{index}",
                "repository": {"full_name": "example/bot"},
            }
            for index in range(5)
        ]
        return _github_response(payload={"items": items}, url=url)

    _install_github_api(monkeypatch, github_provider, response_for)

    class RawClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers):
            raw_requests.append(url)
            return _github_response(
                403,
                {"message": "API rate limit exceeded"},
                {"Retry-After": "2"},
                url,
            )

    monkeypatch.setattr(github_provider.httpx, "AsyncClient", RawClient)
    service = _make_github_service(github_provider, monkeypatch)

    with pytest.raises(github_provider.GithubRateLimitError) as raised:
        await service.search('filename:.env "TELEGRAM_BOT_TOKEN"')

    assert raised.value.wait_seconds == 2
    assert len(raw_requests) == 1


@pytest.mark.asyncio
async def test_github_scanner_warns_and_returns_when_login_is_missing(monkeypatch, caplog):
    from app.infrastructure.scanners.providers import github as github_provider
    from app.workers.tasks.scanners.providers import github as github_task

    redis_client = _GithubRedis()
    _install_github_redis(monkeypatch, redis_client, github_task)
    monkeypatch.setattr(github_provider.settings, "GITHUB_TOKENS", "", raising=False)
    service = github_provider.GithubService()
    service.token = ""

    def no_request(**_kwargs):
        pytest.fail("GitHub request started without a login token")

    monkeypatch.setattr(github_provider, "get_async_http_client", no_request)
    monkeypatch.setattr(github_task, "github", service)
    monkeypatch.setenv("GITHUB_DORK_CAP", "1")

    with caplog.at_level(logging.WARNING, logger="scanner.tasks"):
        result = await github_task._scan_github_async("manual search")

    assert result == "GitHub login token missing"
    assert redis_client.retry_queries == {"manual search"}
    assert "Login token is missing or empty" in caplog.text


@pytest.mark.asyncio
async def test_github_shared_search_timer_waits_for_the_next_slot(monkeypatch):
    from app.infrastructure.scanners.providers import github as github_provider

    redis_client = _GithubRedis(waits=[0, 1500, 0])
    _install_github_redis(monkeypatch, redis_client)
    service = _make_github_service(github_provider, monkeypatch)
    sleeps = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(github_provider.asyncio, "sleep", record_sleep)

    await service._wait_for_code_search_slot("first search")
    await service._wait_for_code_search_slot("second search")

    assert sleeps == [1.5]
    assert len(redis_client.eval_calls) == 3


@pytest.mark.asyncio
async def test_github_scanner_passes_rolling_cutoff_to_every_dork(monkeypatch, request):
    from app.infrastructure.scanners.providers import github as github_provider
    from app.workers.tasks.scanners.providers import github as github_task

    redis_client = _GithubRedis()
    _install_github_redis(monkeypatch, redis_client, github_task)
    now = datetime.now(UTC)
    old_push = (now - timedelta(days=2)).isoformat()
    search_queries = []
    raw_urls = []

    async def response_for(url, *, params=None, **_kwargs):
        assert url.endswith("/search/code")
        search_queries.append(params["q"])
        return _github_response(
            payload={
                "items": [{
                    "html_url": "https://github.com/old/bot/blob/main/.env",
                    "path": ".env",
                    "sha": "old-sha",
                    "repository": {"full_name": "old/bot", "pushed_at": old_push},
                }],
                "incomplete_results": False,
            },
        )

    _install_github_api(monkeypatch, github_provider, response_for)

    class RawResponse:
        status_code = 200
        headers = {}
        text = ""

        @staticmethod
        def raise_for_status():
            return None

    class RawClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers):
            raw_urls.append(url)
            return RawResponse()

    monkeypatch.setattr(github_provider.httpx, "AsyncClient", RawClient)
    service = _make_github_service(github_provider, monkeypatch)

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setenv("GITHUB_DORK_CAP", "2")
    monkeypatch.setenv("GITHUB_QUERY_SLEEP", "0")
    monkeypatch.setenv("GITHUB_PUSHED_WITHIN_DAYS", "1")
    breaker = github_task._cb("github")
    breaker.reset()
    request.addfinalizer(breaker.reset)
    monkeypatch.setattr(github_task, "github", service)
    monkeypatch.setattr(github_task, "_send_log_async", no_op)
    monkeypatch.setattr(github_task.asyncio, "sleep", no_op)

    await github_task._scan_github_async()

    assert search_queries == [
        'filename:.env "TELEGRAM_BOT_TOKEN"',
        'filename:.env "TG_BOT_TOKEN"',
    ]
    assert raw_urls == []
    breaker.reset()


@pytest.mark.asyncio
async def test_github_scanner_retries_failed_search_and_carries_on(monkeypatch, request, caplog):
    from app.infrastructure.scanners.providers import github as github_provider
    from app.workers.tasks.scanners.providers import github as github_task

    query_a = 'filename:.env "TELEGRAM_BOT_TOKEN"'
    query_b = 'filename:.env "TG_BOT_TOKEN"'
    redis_client = _GithubRedis()
    _install_github_redis(monkeypatch, redis_client, github_task)
    search_queries = []
    failed_once = False

    async def response_for(url, *, params=None, **_kwargs):
        nonlocal failed_once
        search_query = params["q"]
        search_queries.append(search_query)
        if search_query == query_a and not failed_once:
            failed_once = True
            return _github_response(500, {"message": "temporary GitHub outage"}, url=url)
        return _github_response(
            payload={"items": [], "incomplete_results": False},
            url=url,
        )

    _install_github_api(monkeypatch, github_provider, response_for)
    service = _make_github_service(github_provider, monkeypatch)
    breaker = github_task._cb("github")
    breaker.reset()
    request.addfinalizer(breaker.reset)

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setenv("GITHUB_DORK_CAP", "2")
    monkeypatch.setenv("GITHUB_QUERY_SLEEP", "0")
    monkeypatch.setattr(github_task, "github", service)
    monkeypatch.setattr(github_task, "_send_log_async", no_op)
    monkeypatch.setattr(github_task.asyncio, "sleep", no_op)

    with caplog.at_level(logging.ERROR, logger="scanner.tasks"):
        await github_task._scan_github_async()
        assert redis_client.retry_queries == {query_a}
        await github_task._scan_github_async()

    assert search_queries == [query_a, query_b, query_a, query_b]
    assert redis_client.retry_queries == set()
    assert f"Search {query_a!r} failed" in caplog.text
    assert "Saved it for a later scan; continuing with the other searches" in caplog.text


@pytest.mark.asyncio
async def test_github_scanner_waits_and_stops_after_rate_limit(monkeypatch, request, caplog):
    from app.infrastructure.scanners.providers import github as github_provider
    from app.workers.tasks.scanners.providers import github as github_task

    query_a = 'filename:.env "TELEGRAM_BOT_TOKEN"'
    redis_client = _GithubRedis()
    _install_github_redis(monkeypatch, redis_client, github_task)
    search_queries = []
    sleeps = []

    async def response_for(url, *, params=None, **_kwargs):
        search_queries.append(params["q"])
        return _github_response(
            403,
            {"message": "API rate limit exceeded"},
            {"Retry-After": "3"},
            url,
        )

    _install_github_api(monkeypatch, github_provider, response_for)
    service = _make_github_service(github_provider, monkeypatch)
    breaker = github_task._cb("github")
    breaker.reset()
    request.addfinalizer(breaker.reset)

    async def record_sleep(seconds):
        sleeps.append(seconds)

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setenv("GITHUB_DORK_CAP", "2")
    monkeypatch.setenv("GITHUB_QUERY_SLEEP", "0")
    monkeypatch.setattr(github_task, "github", service)
    monkeypatch.setattr(github_task, "_send_log_async", no_op)
    monkeypatch.setattr(github_task.asyncio, "sleep", record_sleep)

    with caplog.at_level(logging.ERROR, logger="scanner.tasks"):
        await github_task._scan_github_async()

    assert search_queries == [query_a]
    assert redis_client.retry_queries == {query_a}
    assert sleeps == [3.0]
    assert "Waiting 3.0s, then stopping this scan" in caplog.text
