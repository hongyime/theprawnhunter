import pytest

from app.workers.tasks.flows import api_keys as task


class _Redis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


class _Broadcaster:
    def __init__(self):
        self.logs = []

    async def send_log(self, message):
        self.logs.append(message)
        return True


@pytest.mark.asyncio
async def test_alerts_only_on_transitions(monkeypatch):
    redis = _Redis()
    broadcaster = _Broadcaster()
    monkeypatch.setattr(task, "redis_client", redis)

    import app.workers.tasks.flow_tasks as flow_tasks

    monkeypatch.setattr(flow_tasks, "get_broadcaster", lambda: broadcaster)

    rows = [
        {"provider": "google_cse", "ok": False, "detail": "HTTP 403"},
        {"provider": "exa", "ok": True, "detail": "HTTP 200"},
        {"provider": "fofa", "ok": False, "detail": "not configured"},
    ]

    async def fake_check_all():
        return rows

    import app.services.api_key_health as health

    monkeypatch.setattr(health, "check_all", fake_check_all)

    first = await task._api_key_health_async()
    assert first == "api_key_health:checked=2,failing=1"
    assert len(broadcaster.logs) == 1
    assert "google_cse" in broadcaster.logs[0]

    second = await task._api_key_health_async()
    assert second == "api_key_health:checked=2,failing=1"
    assert len(broadcaster.logs) == 1, "steady state must not re-alert"

    rows[0]["ok"] = True
    rows[0]["detail"] = "HTTP 200"
    third = await task._api_key_health_async()
    assert third == "api_key_health:checked=2,failing=0"
    assert len(broadcaster.logs) == 2
    assert "recovered" in broadcaster.logs[1]


@pytest.mark.asyncio
async def test_check_errors_do_not_raise(monkeypatch):
    import app.services.api_key_health as health

    async def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(health, "check_all", boom)
    assert await task._api_key_health_async() == "api_key_health:error"
