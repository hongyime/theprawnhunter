import json

import httpx
import pytest

from app.services import virustotal_srv
from app.services.virustotal_srv import VirusTotalService, url_identifier


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def incr(self, key):
        self.store[key] = str(int(self.store.get(key, 0)) + 1)

    def expire(self, key, ttl):
        return True

    def pipeline(self):
        outer = self

        class _Pipe:
            def incr(self, key):
                outer.incr(key)

            def expire(self, key, ttl):
                return True

            def execute(self):
                return []

        return _Pipe()


class _FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def _client(response):
    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            return response

    return _Ctx()


def _verdict_payload(malicious=3, suspicious=1):
    return {
        "data": {
            "attributes": {
                "last_analysis_stats": {
                    "malicious": malicious,
                    "suspicious": suspicious,
                    "harmless": 60,
                    "undetected": 5,
                },
                "reputation": -42,
                "categories": {"test": "malware"},
            }
        }
    }


def test_url_identifier_matches_virustotal_format():
    assert url_identifier("https://example.com/a") == "aHR0cHM6Ly9leGFtcGxlLmNvbS9h"


@pytest.mark.asyncio
async def test_domain_report_normalises_stats(monkeypatch):
    service = VirusTotalService()
    service.api_key = "test-key"
    service._redis = _FakeRedis()
    monkeypatch.setattr(
        virustotal_srv.httpx, "AsyncClient", lambda **kw: _client(_FakeResponse(200, _verdict_payload()))
    )

    verdict = await service.domain_report("evil.example")

    assert verdict["malicious"] == 3
    assert verdict["suspicious"] == 1
    assert verdict["reputation"] == -42
    assert verdict["categories"] == {"test": "malware"}


@pytest.mark.asyncio
async def test_second_lookup_uses_cache(monkeypatch):
    service = VirusTotalService()
    service.api_key = "test-key"
    service._redis = _FakeRedis()
    calls = []

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            calls.append(url)
            return _FakeResponse(200, _verdict_payload())

    monkeypatch.setattr(virustotal_srv.httpx, "AsyncClient", lambda **kw: _Ctx())

    await service.domain_report("evil.example")
    await service.domain_report("evil.example")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_minute_budget_stops_requests(monkeypatch):
    service = VirusTotalService()
    service.api_key = "test-key"
    redis = _FakeRedis()
    service._redis = redis
    from datetime import UTC, datetime

    minute = datetime.now(UTC).strftime("%Y%m%d%H%M")
    redis.store[f"vt:minute:{minute}"] = "4"
    monkeypatch.setattr(
        virustotal_srv.httpx,
        "AsyncClient",
        lambda **kw: pytest.fail("HTTP must not be called when the budget is spent"),
    )

    assert await service.domain_report("evil.example") is None


@pytest.mark.asyncio
async def test_rate_limit_sets_cooldown(monkeypatch):
    service = VirusTotalService()
    service.api_key = "test-key"
    redis = _FakeRedis()
    service._redis = redis
    monkeypatch.setattr(
        virustotal_srv.httpx,
        "AsyncClient",
        lambda **kw: _client(_FakeResponse(429, headers={"Retry-After": "90"})),
    )

    assert await service.domain_report("evil.example") is None
    assert redis.store.get("vt:cooldown") == "1"


@pytest.mark.asyncio
async def test_rejected_key_disables_enrichment(monkeypatch):
    service = VirusTotalService()
    service.api_key = "bad-key"
    service._redis = _FakeRedis()
    monkeypatch.setattr(
        virustotal_srv.httpx, "AsyncClient", lambda **kw: _client(_FakeResponse(401))
    )

    assert await service.domain_report("evil.example") is None
    assert service.enabled is False


def test_host_and_staleness_helpers():
    from app.workers.tasks.flows import virustotal as task

    assert task._host_from_url("https://Evil.Example/x") == "evil.example"
    assert task._host_from_url("not a url") is None
    assert task._is_stale({}) is True
    assert task._is_stale({"vt_checked_at": "2999-01-01T00:00:00+00:00"}) is False
