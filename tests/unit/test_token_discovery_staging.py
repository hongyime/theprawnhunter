"""Captured-message token discovery must stage, never broker, plaintext.

F2 7.7 regression guard: ``_enqueue_tokens_from_messages`` used to call
``validate_token.apply_async(args=[{"token": ...}])``, which put plaintext bot
tokens on the Celery broker. It now hands candidates to
``_save_credentials_async``, which encrypts them into ``validation_queue``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.workers.tasks.flows import scrape

TOKEN_A = "1234567890:AA" + ("x" * 33)
TOKEN_B = "9876543210:BB" + ("y" * 33)


class _FakeRedis:
    def __init__(self):
        self.keys: set[str] = set()

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


@pytest.fixture
def staged(monkeypatch):
    calls: list[dict] = []

    async def fake_save(entries, source_name):
        entries = list(entries)
        calls.append({"entries": entries, "source": source_name})
        return len(entries)

    monkeypatch.setattr(
        "app.workers.tasks.scanners.common._save_credentials_async", fake_save
    )
    return calls


@pytest.fixture
def broker(monkeypatch):
    task = MagicMock()
    monkeypatch.setattr("app.workers.tasks.validation_tasks.validate_token", task)
    return task


@pytest.mark.asyncio
async def test_tokens_are_staged_not_sent_to_broker(monkeypatch, staged, broker):
    monkeypatch.setattr(scrape, "redis_client", _FakeRedis())
    rows = [
        {
            "id": "msg-1",
            "credential_id": "cred-1",
            "telegram_msg_id": 42,
            "content": f"leak {TOKEN_A} chat_id=-100123",
        }
    ]

    assert await scrape._enqueue_tokens_from_messages(rows) == 1

    assert len(staged) == 1
    assert staged[0]["source"] == "telegram_message"
    assert staged[0]["entries"] == [
        {
            "token": TOKEN_A,
            "chat_id": "-100123",
            "meta": {
                "source": "telegram_message",
                "credential_id": "cred-1",
                "message_id": "msg-1",
                "telegram_msg_id": 42,
            },
        }
    ]
    broker.apply_async.assert_not_called()
    broker.delay.assert_not_called()


@pytest.mark.asyncio
async def test_same_token_twice_in_batch_is_staged_once(monkeypatch, staged, broker):
    monkeypatch.setattr(scrape, "redis_client", _FakeRedis())
    rows = [
        {"id": "m1", "credential_id": "c1", "content": TOKEN_A},
        {"id": "m2", "credential_id": "c1", "content": f"{TOKEN_A} {TOKEN_B}"},
    ]

    assert await scrape._enqueue_tokens_from_messages(rows) == 2

    assert [entry["token"] for entry in staged[0]["entries"]] == [TOKEN_A, TOKEN_B]
    broker.apply_async.assert_not_called()


@pytest.mark.asyncio
async def test_staging_failure_propagates_for_task_retry(monkeypatch, broker):
    async def fail(entries, source_name):
        raise RuntimeError("queue down")

    monkeypatch.setattr("app.workers.tasks.scanners.common._save_credentials_async", fail)
    monkeypatch.setattr(scrape, "redis_client", _FakeRedis())
    rows = [{"id": "m1", "credential_id": "c1", "content": TOKEN_A}]

    with pytest.raises(RuntimeError, match="queue down"):
        await scrape._enqueue_tokens_from_messages(rows)

    broker.apply_async.assert_not_called()


@pytest.mark.asyncio
async def test_no_tokens_does_not_stage(monkeypatch, staged, broker):
    monkeypatch.setattr(scrape, "redis_client", _FakeRedis())
    rows = [{"id": "m1", "credential_id": "c1", "content": "nothing to see"}]
    rows_without_content = [{"id": "m2", "credential_id": "c1", "content": None}]

    assert await scrape._enqueue_tokens_from_messages(rows) == 0
    assert await scrape._enqueue_tokens_from_messages(rows_without_content) == 0
    assert staged == []
    broker.apply_async.assert_not_called()
