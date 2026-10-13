from datetime import UTC, datetime, timedelta

import pytest


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
        text = ""

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
    service = github_provider.GithubService()
    monkeypatch.setattr(service, "_get_token", lambda: "test-token")

    await service.search(
        'filename:.env "TELEGRAM_BOT_TOKEN"',
        pushed_since=service.recent_push_cutoff(now),
    )

    assert raw_urls == ["https://raw.githubusercontent.com/new/bot/main/.env"]


@pytest.mark.asyncio
async def test_github_scanner_passes_rolling_cutoff_to_every_dork(monkeypatch):
    from app.workers.tasks.scanners.providers import github as github_task

    cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    search_calls = []

    class RedisClient:
        @staticmethod
        def get(_key):
            return None

    class GithubService:
        @staticmethod
        def recent_push_cutoff():
            return cutoff

        @staticmethod
        async def search(query, *, pushed_since):
            search_calls.append((query, pushed_since))
            return []

    class CircuitBreaker:
        @staticmethod
        def call(function):
            return function

    async def no_op(*_args, **_kwargs):
        return None

    async def save_no_op(*_args, **_kwargs):
        return 0

    monkeypatch.setenv("GITHUB_DORK_CAP", "2")
    monkeypatch.setenv("GITHUB_QUERY_SLEEP", "0")
    monkeypatch.setattr(github_task, "redis_client", RedisClient())
    monkeypatch.setattr(github_task, "github", GithubService())
    monkeypatch.setattr(github_task, "_cb", lambda _name: CircuitBreaker())
    monkeypatch.setattr(github_task, "_send_log_async", no_op)
    monkeypatch.setattr(github_task, "_save_credentials_async", save_no_op)
    monkeypatch.setattr(github_task.asyncio, "sleep", no_op)

    await github_task._scan_github_async()

    assert len(search_calls) == 2
    assert all(pushed_since is cutoff for _, pushed_since in search_calls)
