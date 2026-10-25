"""Dispatcher tests for ``validation.dispatch_pending``.

The queue service owns atomic leasing and priority ordering; these tests pin the
dispatcher's contract: reclaim expired leases, cap the lease at the batch and the
shared getMe bucket, preserve the lease-returned order, and never enqueue a row
without a plaintext token.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.workers.tasks import validation_tasks

RATE_KEY = validation_tasks.RATE_LIMIT_KEY


class _FakePipeline:
    def __init__(self, fake: _FakeRedis):
        self._fake = fake
        self._ops: list[tuple[str, str]] = []

    def get(self, key: str):
        self._ops.append(("get", key))
        return self

    def ttl(self, key: str):
        self._ops.append(("ttl", key))
        return self

    def execute(self):
        return [
            self._fake.get(key) if op == "get" else self._fake.ttl(key)
            for op, key in self._ops
        ]


class _FakeRedis:
    def __init__(self, *, used: int | None = None, ttl: int = -2, paused: bool = False):
        self.data = {}
        if used is not None:
            self.data[RATE_KEY] = str(used)
        self.ttl_value = ttl
        self.paused = paused

    def get(self, key: str):
        if key == "system:paused":
            return "1" if self.paused else None
        return self.data.get(key)

    def ttl(self, key: str):
        return self.ttl_value if key in self.data else -2

    def pipeline(self):
        return _FakePipeline(self)


@pytest.fixture
def queue_calls():
    return []


@pytest.fixture
def sent():
    return []


def _install_queue(monkeypatch, *, leased, calls):
    async def release_expired():
        calls.append(("release_expired",))
        return 0

    async def lease(limit, seconds):
        calls.append(("lease", limit, seconds))
        return list(leased)[:limit]

    async def complete(lease_id, results):
        calls.append(("complete", lease_id, results))
        return 1

    monkeypatch.setattr(validation_tasks.validation_queue_srv, "release_expired", release_expired)
    monkeypatch.setattr(validation_tasks.validation_queue_srv, "lease", lease)
    monkeypatch.setattr(validation_tasks.validation_queue_srv, "complete", complete)


def _install_enqueue(monkeypatch, sent):
    def apply_async(*, args, kwargs, queue):
        sent.append({"args": args, "kwargs": kwargs, "queue": queue})

    monkeypatch.setattr(
        validation_tasks, "validate_token", SimpleNamespace(apply_async=apply_async)
    )


def _row(index: int, *, token: str | None = None, source: str = "github") -> dict:
    return {
        "queue_row_id": f"row-{index}",
        "token": f"{index}000000000:AA{'x' * 30}" if token is None else token,
        "source": source,
        "meta": {"index": index},
        "attempts": 0,
        "lease_id": f"lease-{index}",
    }


def _set_batch(monkeypatch, batch: int, *, lease_seconds: int = 300):
    monkeypatch.setattr(settings, "VALIDATION_DISPATCH_BATCH", batch, raising=False)
    monkeypatch.setattr(settings, "VALIDATION_LEASE_SECONDS", lease_seconds, raising=False)


def _set_redis(monkeypatch, fake: _FakeRedis):
    monkeypatch.setattr(validation_tasks, "redis_client", fake)


@pytest.mark.asyncio
async def test_batch_limit_preserves_priority_order_from_lease(monkeypatch, queue_calls, sent):
    _set_batch(monkeypatch, 5)
    _set_redis(monkeypatch, _FakeRedis())
    rows = [_row(index) for index in range(5)]
    _install_queue(monkeypatch, leased=rows, calls=queue_calls)
    _install_enqueue(monkeypatch, sent)

    assert await validation_tasks._dispatch_pending_async() == "dispatched:5"

    assert queue_calls[0] == ("release_expired",)
    assert ("lease", 5, 300) in queue_calls
    # lease() returns rows highest-priority first; the dispatcher must preserve
    # that order instead of re-sorting or fanning out concurrently.
    assert [message["args"][0]["meta"]["index"] for message in sent] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_batch_limit_caps_lease_requests(monkeypatch, queue_calls, sent):
    _set_batch(monkeypatch, 2)
    _set_redis(monkeypatch, _FakeRedis())
    _install_queue(monkeypatch, leased=[_row(index) for index in range(10)], calls=queue_calls)
    _install_enqueue(monkeypatch, sent)

    assert await validation_tasks._dispatch_pending_async() == "dispatched:2"
    assert ("lease", 2, 300) in queue_calls
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_bucket_exhaustion_stops_dispatch_without_leasing(monkeypatch, queue_calls, sent):
    _set_batch(monkeypatch, 50)
    _set_redis(
        monkeypatch,
        _FakeRedis(used=validation_tasks.RATE_MAX_CALLS, ttl=7),
    )
    _install_queue(monkeypatch, leased=[_row(0)], calls=queue_calls)
    _install_enqueue(monkeypatch, sent)

    assert await validation_tasks._dispatch_pending_async() == "rate_limited:0"
    assert queue_calls == [("release_expired",)]
    assert sent == []


@pytest.mark.asyncio
async def test_partial_bucket_caps_lease_below_batch(monkeypatch, queue_calls, sent):
    _set_batch(monkeypatch, 50)
    remaining = 2
    _set_redis(
        monkeypatch,
        _FakeRedis(used=validation_tasks.RATE_MAX_CALLS - remaining, ttl=7),
    )
    _install_queue(monkeypatch, leased=[_row(index) for index in range(10)], calls=queue_calls)
    _install_enqueue(monkeypatch, sent)

    assert await validation_tasks._dispatch_pending_async() == "dispatched:2"
    assert ("lease", remaining, 300) in queue_calls


@pytest.mark.asyncio
async def test_empty_queue_no_send_no_error(monkeypatch, queue_calls, sent):
    _set_batch(monkeypatch, 50)
    _set_redis(monkeypatch, _FakeRedis())
    _install_queue(monkeypatch, leased=[], calls=queue_calls)
    _install_enqueue(monkeypatch, sent)

    assert await validation_tasks._dispatch_pending_async() == "dispatched:0"
    assert queue_calls[0] == ("release_expired",)
    # Batch 50 is capped by the shared getMe bucket (30 by default).
    assert ("lease", min(50, validation_tasks.RATE_MAX_CALLS), 300) in queue_calls
    assert sent == []


@pytest.mark.asyncio
async def test_enqueue_payload_carries_queue_row_id_and_no_plaintext_kwargs(
    monkeypatch, queue_calls, sent
):
    _set_batch(monkeypatch, 1)
    _set_redis(monkeypatch, _FakeRedis())
    row = _row(0, source="github:code_search")
    _install_queue(monkeypatch, leased=[row], calls=queue_calls)
    _install_enqueue(monkeypatch, sent)

    await validation_tasks._dispatch_pending_async()

    message = sent[0]
    assert message["queue"] == "validation"
    assert message["kwargs"] == {"queue_row_id": "row-0"}
    assert "token" not in message["kwargs"]
    # The positional token keeps the legacy task contract; the queue row id is
    # what makes the encrypted row authoritative inside validate_token.
    assert message["args"][0]["token"] == row["token"]
    assert message["args"][1] == "github:code_search"


@pytest.mark.asyncio
async def test_row_without_plaintext_token_is_failed_not_enqueued(
    monkeypatch, queue_calls, sent, caplog
):
    _set_batch(monkeypatch, 5)
    _set_redis(monkeypatch, _FakeRedis())
    _install_queue(monkeypatch, leased=[_row(0, token="")], calls=queue_calls)
    _install_enqueue(monkeypatch, sent)
    caplog.set_level("WARNING", logger="validation.tasks")

    assert await validation_tasks._dispatch_pending_async() == "dispatched:0"
    assert sent == []
    assert (
        "complete",
        "lease-0",
        [{"queue_row_id": "row-0", "success": False}],
    ) in queue_calls
    assert "no plaintext token" in caplog.text


@pytest.mark.asyncio
async def test_zero_batch_does_not_lease(monkeypatch, queue_calls, sent):
    _set_batch(monkeypatch, 0)
    _set_redis(monkeypatch, _FakeRedis())
    _install_queue(monkeypatch, leased=[_row(0)], calls=queue_calls)
    _install_enqueue(monkeypatch, sent)

    assert await validation_tasks._dispatch_pending_async() == "dispatched:0"
    assert ("lease", 0, 300) not in queue_calls
    assert all(call[0] != "lease" for call in queue_calls)
    assert sent == []


@pytest.mark.asyncio
async def test_paused_system_skips_dispatch(monkeypatch, queue_calls, sent):
    _set_batch(monkeypatch, 50)
    _set_redis(monkeypatch, _FakeRedis(paused=True))
    _install_queue(monkeypatch, leased=[_row(0)], calls=queue_calls)
    _install_enqueue(monkeypatch, sent)

    assert await validation_tasks._dispatch_pending_async() == "paused"
    assert queue_calls == [("release_expired",)]
    assert sent == []
