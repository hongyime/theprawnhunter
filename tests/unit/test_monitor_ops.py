"""Unit tests for /monitor/ops status, pause and resume."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routers._monitor import ops as ops_mod
from app.core.queue_monitor import DEFAULT_QUEUES

AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}
HEALTHY_LENGTHS = {"celery": 3, "scrape": 5, "scanners": 0, "validation": 2}


class _FakeRedis:
    """Minimal sync Redis double for the operations helpers."""

    def __init__(
        self,
        *,
        paused: bool = False,
        fail: bool = False,
        lengths: dict[str, int] | None = None,
        bot_ids: tuple[str, ...] = (),
    ):
        self.paused = paused
        self.fail = fail
        self.lengths = lengths or {}
        self.bot_ids = bot_ids
        self.calls: list[tuple] = []

    def ping(self):
        if self.fail:
            raise RuntimeError("redis down")

    def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append(("get", key))
        return "true" if self.paused else None

    def set(self, key, value):
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append(("set", key, value))
        return True

    def delete(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append(("delete", key))
        return 1

    def llen(self, queue):
        if self.fail:
            raise RuntimeError("redis down")
        return self.lengths.get(queue, 0)

    def zrange(self, *args, **kwargs):
        return []

    def lindex(self, *args, **kwargs):
        return None

    def scan_iter(self, match=None, count=None):
        if self.fail:
            raise RuntimeError("redis down")
        return iter([f"bot_listener:poll_lock:{bot_id}" for bot_id in self.bot_ids])


def _use_redis(monkeypatch, fake: _FakeRedis) -> _FakeRedis:
    monkeypatch.setattr(ops_mod, "_redis_client", lambda: fake)
    return fake


def _use_pending(monkeypatch, value: int | None = 7):
    counter = AsyncMock(return_value=value)
    monkeypatch.setattr(ops_mod, "_exact_count_async", counter)
    return counter


def _local_client() -> TestClient:
    app = FastAPI()
    app.include_router(ops_mod.router, prefix="/monitor")
    return TestClient(app)


def test_status_shape_reports_pause_health_and_depths(monkeypatch):
    fake = _use_redis(
        monkeypatch,
        _FakeRedis(
            paused=True,
            lengths=HEALTHY_LENGTHS,
            bot_ids=("111", "222"),
        ),
    )
    _use_pending(monkeypatch, 7)

    status = asyncio.run(ops_mod.ops_status())

    assert status.paused is True
    assert status.redis == "ok"
    assert status.pending_broadcasts == 7
    assert status.queue_lengths.model_dump() == {
        "celery": 3,
        "scrape": 5,
        "scanners": 0,
        "validation": 2,
    }
    assert status.bot_count == 2
    assert ("get", ops_mod.PAUSE_KEY) in fake.calls


def test_status_redis_failure_degrades_to_200_payload(monkeypatch):
    _use_redis(monkeypatch, _FakeRedis(fail=True))
    _use_pending(monkeypatch, 11)

    status = asyncio.run(ops_mod.ops_status())

    # Redis is down: still a normal model, not an HTTPException/500.
    assert status.redis == "unreachable"
    assert status.paused is False
    assert status.pending_broadcasts == 11
    assert status.queue_lengths.model_dump() == {queue: 0 for queue in DEFAULT_QUEUES}
    assert status.bot_count is None


def test_status_pending_broadcast_failure_is_null(monkeypatch):
    _use_redis(monkeypatch, _FakeRedis(lengths=HEALTHY_LENGTHS))

    async def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(ops_mod, "_exact_count_async", _boom)

    status = asyncio.run(ops_mod.ops_status())

    assert status.pending_broadcasts is None
    assert status.redis == "ok"


def test_pause_writes_the_bot_pause_key_and_audits(monkeypatch):
    fake = _use_redis(monkeypatch, _FakeRedis())
    audit = MagicMock()
    monkeypatch.setattr(ops_mod, "AuditLogger", SimpleNamespace(log=audit))

    result = asyncio.run(ops_mod.ops_pause())

    assert result.paused is True
    assert ("set", ops_mod.PAUSE_KEY, "true") in fake.calls
    audit.assert_called_once()
    _, kwargs = audit.call_args
    assert audit.call_args.args[0] == ops_mod.OPS_PAUSE_EVENT
    assert kwargs["user"] == "monitor-api"


def test_resume_deletes_the_bot_pause_key_and_audits(monkeypatch):
    fake = _use_redis(monkeypatch, _FakeRedis(paused=True))
    audit = MagicMock()
    monkeypatch.setattr(ops_mod, "AuditLogger", SimpleNamespace(log=audit))

    result = asyncio.run(ops_mod.ops_resume())

    assert result.paused is False
    assert ("delete", ops_mod.PAUSE_KEY) in fake.calls
    audit.assert_called_once()
    assert audit.call_args.args[0] == ops_mod.OPS_RESUME_EVENT
    assert audit.call_args.kwargs["user"] == "monitor-api"


def test_pause_and_resume_report_redis_unavailable(monkeypatch):
    _use_redis(monkeypatch, _FakeRedis(fail=True))

    for action in (ops_mod.ops_pause, ops_mod.ops_resume):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(action())
        assert exc_info.value.status_code == 503
        assert "key" not in str(exc_info.value.detail).lower()


def test_monitor_key_is_required_for_all_ops_routes(monkeypatch):
    _use_redis(monkeypatch, _FakeRedis(lengths=HEALTHY_LENGTHS, bot_ids=("1",)))
    _use_pending(monkeypatch, 0)
    audit = MagicMock()
    monkeypatch.setattr(ops_mod, "AuditLogger", SimpleNamespace(log=audit))

    client = _local_client()

    # Missing and wrong keys are rejected before touching Redis or the DB.
    assert client.get("/monitor/ops/status").status_code == 403
    assert (
        client.get(
            "/monitor/ops/status", headers={"X-Monitor-Key": "not-the-key"}
        ).status_code
        == 403
    )
    assert client.post("/monitor/ops/pause").status_code == 403
    assert (
        client.post(
            "/monitor/ops/pause", headers={"X-Monitor-Key": "not-the-key"}
        ).status_code
        == 403
    )
    assert client.post("/monitor/ops/resume").status_code == 403
    assert audit.call_count == 0

    # The correct key reaches the handler and returns the documented shape.
    status = client.get("/monitor/ops/status", headers=AUTH)
    assert status.status_code == 200
    assert set(status.json()) == {
        "paused",
        "redis",
        "pending_broadcasts",
        "queue_lengths",
        "bot_count",
    }
    assert set(status.json()["queue_lengths"]) == set(DEFAULT_QUEUES)

    assert client.post("/monitor/ops/pause", headers=AUTH).json() == {"paused": True}
    assert client.post("/monitor/ops/resume", headers=AUTH).json() == {"paused": False}
    assert audit.call_count == 2
