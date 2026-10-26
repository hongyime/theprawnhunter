import httpx
import pytest

from app.infrastructure.scanners.providers import searxng as mod
from app.infrastructure.scanners.providers.searxng import SearxngService

TOKEN = "1234567890:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"


class _Proxy:
    def __init__(self, handler):
        self.handler = handler

    def __getattr__(self, name):
        if name == "AsyncClient":
            real = httpx.AsyncClient

            def build(**kwargs):
                return real(transport=httpx.MockTransport(self.handler), **kwargs)

            return build
        return getattr(httpx, name)


def _install(monkeypatch, handler):
    monkeypatch.setattr(mod, "httpx", _Proxy(handler))
    return SearxngService()


@pytest.mark.asyncio
async def test_extracts_tokens_from_result_snippets(monkeypatch):
    def handler(request):
        assert "format=json" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "leaked config",
                        "content": f"BOT_TOKEN={TOKEN} chat_id=-1001234567890",
                        "url": "https://paste.example/abc",
                        "engine": "google cse",
                    }
                ]
            },
        )

    service = _install(monkeypatch, handler)
    results = await service.search(query='"api.telegram.org/bot"')

    assert [r["token"] for r in results] == [TOKEN]
    assert results[0]["meta"]["source"] == "searxng"
    assert results[0]["meta"]["url"] == "https://paste.example/abc"
    assert results[0]["meta"]["chat_id"] == "-1001234567890"


@pytest.mark.asyncio
async def test_non_200_returns_empty_without_raising(monkeypatch):
    service = _install(monkeypatch, lambda request: httpx.Response(429))
    assert await service.search(query="anything") == []


@pytest.mark.asyncio
async def test_bad_json_is_skipped(monkeypatch):
    service = _install(monkeypatch, lambda request: httpx.Response(200, text="<html>nope</html>"))
    assert await service.search(query="anything") == []


@pytest.mark.asyncio
async def test_results_are_capped_per_query(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": f"r{i}", "content": "nothing here", "url": f"https://x/{i}"}
                    for i in range(50)
                ]
            },
        )

    service = _install(monkeypatch, handler)
    assert await service.search(query="q") == []
