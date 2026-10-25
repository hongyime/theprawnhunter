"""Non-text documents must be skipped by the central token sweep call sites."""

from types import SimpleNamespace

import pytest


class _MemoryRedis:
    def __init__(self):
        self.values = {}
        self.exists_calls = []

    def get(self, key):
        return self.values.get(key)

    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    @staticmethod
    def expire(_key, _seconds):
        return True

    def exists(self, key):
        self.exists_calls.append(key)
        return int(key in self.values)

    def setex(self, key, _ttl, value):
        self.values[key] = value
        return True


@pytest.mark.asyncio
async def test_netlas_bytes_body_is_skipped_without_raising(monkeypatch):
    import netlas as netlas_sdk

    from app.infrastructure.scanners.providers.netlas import NetlasService

    redis = _MemoryRedis()
    good_token = f"12345678:{'Z' * 35}"

    class FakeNetlas:
        def __init__(self, *, api_key):
            assert api_key == "fake-netlas-key"

        def query(self, **_kwargs):
            return {
                "items": [
                    {
                        "data": {
                            "http": {"body": b"\x00\x01binary\x00blob"},
                            "ip": "203.0.113.10",
                            "port": 443,
                        }
                    },
                    {
                        "data": {
                            "http": {"body": f"chat_id=-1001 bot{good_token}"},
                            "ip": "203.0.113.11",
                            "port": 443,
                        }
                    },
                ]
            }

    service = NetlasService()
    service.keys = [(1, "fake-netlas-key")]
    monkeypatch.setattr(service, "_redis", lambda: redis)
    monkeypatch.setattr(netlas_sdk, "Netlas", FakeNetlas)

    results = await service.search("http.body:telegram")

    assert [item["token"] for item in results] == [good_token]
    assert results[0]["chat_id"] == "-1001"
    assert results[0]["meta"]["ip"] == "203.0.113.11"


@pytest.mark.asyncio
async def test_commoncrawl_non_text_url_is_skipped_without_raising(monkeypatch):
    from app.core.redis_srv import redis_srv
    from app.infrastructure.scanners.providers import commoncrawl

    good_token = f"23456789:{'Y' * 35}"
    index_body = "\n".join(
        [
            '{"url": 12345, "timestamp": "20260101000000"}',
            '{"url": "https://api.telegram.org/bot%s/sendMessage", "timestamp": "20260101000000"}'
            % good_token,
        ]
    )

    class FakeResponse:
        def __init__(self, payload=None, text=""):
            self.status_code = 200
            self._payload = payload
            self.text = text

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **_kwargs):
            if url == commoncrawl.CommonCrawlService.COLLINFO_URL:
                return FakeResponse(
                    payload=[{"id": "CC-MAIN-2026-01", "cdx-api": "https://index.test/cdx"}]
                )
            return FakeResponse(text=index_body)

    redis = _MemoryRedis()
    monkeypatch.setattr(redis_srv, "_client", redis)
    monkeypatch.setattr(
        commoncrawl,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **_kwargs: FakeClient(),
            Timeout=lambda *_args, **_kwargs: None,
        ),
    )

    results = await commoncrawl.CommonCrawlService().search()

    assert [item["token"] for item in results] == [good_token]
    assert results[0]["meta"]["commoncrawl_url"].endswith("/sendMessage")
