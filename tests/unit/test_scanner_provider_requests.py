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
