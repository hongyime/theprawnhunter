"""Strict per-topic ordering: never overtake an older message in the same chat."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.workers.tasks.flows import broadcast as b


class _FakeDb:
    def __init__(self, payload, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = []

    def rpc(self, name, params):
        if self.fail:
            raise RuntimeError("rpc down")
        self.calls.append((name, params))
        return SimpleNamespace(name=name, params=params)


@pytest.fixture()
def rows():
    return [
        {"id": "a1", "credential_id": "c1", "telegram_msg_id": 10},
        {"id": "a2", "credential_id": "c1", "telegram_msg_id": 11},
        {"id": "b1", "credential_id": "c2", "telegram_msg_id": 500},
    ]


@pytest.mark.asyncio
async def test_keeps_only_oldest_per_topic(monkeypatch, rows):
    fake = _FakeDb([
        {"credential_id": "c1", "min_msg_id": 10},
        {"credential_id": "c2", "min_msg_id": 500},
    ])

    async def _execute(_request):
        return SimpleNamespace(data=fake.payload)

    monkeypatch.setattr(b, "db", fake)
    monkeypatch.setattr(b, "async_execute", _execute)

    kept = await b._filter_to_oldest_per_topic(rows)
    assert [r["id"] for r in kept] == ["a1", "b1"]
    assert fake.calls[0][0] == "min_pending_message_ids"


@pytest.mark.asyncio
async def test_falls_back_when_rpc_fails(monkeypatch, rows):
    fake = _FakeDb([], fail=True)
    monkeypatch.setattr(b, "db", fake)

    kept = await b._filter_to_oldest_per_topic(rows)
    assert len(kept) == 3


@pytest.mark.asyncio
async def test_can_be_disabled(monkeypatch, rows):
    monkeypatch.setenv("BROADCAST_STRICT_PER_TOPIC_ORDER", "0")
    kept = await b._filter_to_oldest_per_topic(rows)
    assert len(kept) == 3
