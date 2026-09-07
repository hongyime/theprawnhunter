import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.routers import health
from app.core.db_retry import DatabaseHealth


@pytest.fixture
def healthy_dependencies(monkeypatch):
    calls = {}

    def probe(name):
        calls[name] = threading.get_ident()
        return True

    monkeypatch.setattr(DatabaseHealth, "check_connection", lambda: probe("database"))
    redis_client = MagicMock()
    redis_client.__enter__.return_value = redis_client
    redis_client.ping.side_effect = lambda: probe("redis")
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: redis_client)

    response = SimpleNamespace(status_code=200)
    sync_client = MagicMock()
    sync_client.__enter__.return_value = sync_client
    sync_client.get.return_value = response
    monkeypatch.setattr("httpx.Client", lambda *args, **kwargs: sync_client)
    async_client = MagicMock()
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.get = AsyncMock(return_value=response)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: async_client)
    return calls


@pytest.mark.asyncio
async def test_detailed_health_keeps_sync_probes_off_event_loop(healthy_dependencies):
    loop_thread = threading.get_ident()
    result = await health.detailed_health()

    assert result == {
        "status": "healthy",
        "checks": {
            "database": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "telegram_bot": {"status": "healthy"},
        },
    }
    assert set(healthy_dependencies) == {"database", "redis"}
    assert all(thread != loop_thread for thread in healthy_dependencies.values())


@pytest.fixture
def probe_pool(monkeypatch):
    with ThreadPoolExecutor(max_workers=3) as executor:
        monkeypatch.setattr(health, "_health_probe_executor", executor)
        monkeypatch.setattr(health, "_health_probe_futures", {})
        yield


@pytest.mark.asyncio
async def test_slow_probe_is_bounded_shared_and_recovers(monkeypatch, probe_pool):
    monkeypatch.setattr(health, "_HEALTH_PROBE_TIMEOUT_SECONDS", 0.1)
    release = threading.Event()
    calls = []

    def blocked_database():
        calls.append(threading.get_ident())
        release.wait(10)

    try:
        results = await asyncio.gather(
            health._run_health_probe("database", blocked_database),
            health._run_health_probe("database", blocked_database),
        )
        assert results == [{"status": "unhealthy", "error": "timeout"}] * 2
        assert len(calls) == 1
        assert (await health.health_check())["status"] == "healthy"
    finally:
        release.set()
        future = health._health_probe_futures.get("database")
        if future is not None:
            await asyncio.wrap_future(future)

    assert await health._run_health_probe("database", lambda: True) == {"status": "healthy"}


@pytest.mark.asyncio
async def test_detailed_health_reports_dependency_failure_without_secrets(
    monkeypatch, healthy_dependencies, probe_pool
):
    def failing_database():
        raise RuntimeError("private database connection secret")

    monkeypatch.setattr(DatabaseHealth, "check_connection", failing_database)
    with pytest.raises(HTTPException) as failure:
        await health.detailed_health()

    assert failure.value.status_code == 503
    assert failure.value.detail == {
        "status": "degraded",
        "checks": {
            "database": {"status": "unhealthy", "error": "connection_failed"},
            "redis": {"status": "healthy"},
            "telegram_bot": {"status": "healthy"},
        },
    }
