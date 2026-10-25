"""Unit tests for the scanner → validation_queue staging seam.

``_save_credentials_async`` must stage encrypted candidates via
``validation_queue_srv.enqueue`` and never put a plaintext token on the Celery
broker. The 24h Redis soft-dedupe and own-bot filter are preserved exactly.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.workers.tasks.scanners import common

TOKEN_A = "123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
TOKEN_B = "223456789:BBHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
TOKEN_C = "323456789:CCHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"


class _Redis:
    def __init__(self, *, already_seen: set[str] | None = None):
        self.keys: set[str] = set(already_seen or ())
        self.set_calls: list[dict] = []

    def set(self, key, value, *, nx=False, ex=None):
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


def _dedupe_key(token: str) -> str:
    return f"validated:recent:{hashlib.sha256(token.encode()).hexdigest()}"


@pytest.fixture
def staged(monkeypatch):
    """Capture entries handed to ``enqueue`` and let each test override behaviour."""
    calls: list[list[dict]] = []
    behaviour = {"fn": lambda entries: len(entries)}

    async def fake_enqueue(entries):
        entries = list(entries)
        calls.append(entries)
        return behaviour["fn"](entries)

    monkeypatch.setattr("app.services.validation_queue_srv.enqueue", fake_enqueue)
    return SimpleNamespace(calls=calls, behaviour=behaviour)


@pytest.fixture
def broker(monkeypatch):
    """Spy on the old broker fanout so tests can prove it is no longer used."""
    task = MagicMock()
    monkeypatch.setattr("app.workers.tasks.validation_tasks.validate_token", task)
    return task


@pytest.mark.asyncio
async def test_dedupe_skip_keeps_queue_count_unchanged(monkeypatch, staged, broker):
    redis = _Redis(already_seen={_dedupe_key(TOKEN_A)})
    monkeypatch.setattr(common, "redis_client", redis)

    queued = await common._save_credentials_async(
        [{"token": TOKEN_A, "meta": {"repo": "owner/repo"}}], "github"
    )

    assert queued == 0
    assert staged.calls == []
    assert redis.set_calls == [
        {"key": _dedupe_key(TOKEN_A), "value": "1", "nx": True, "ex": 86400}
    ]
    broker.delay.assert_not_called()
    broker.apply_async.assert_not_called()


@pytest.mark.asyncio
async def test_own_bot_token_is_skipped_before_queue(monkeypatch, staged, broker):
    redis = _Redis()
    monkeypatch.setattr(common, "redis_client", redis)
    monkeypatch.setattr(common, "_is_own_bot_token", lambda token: token == TOKEN_A)

    queued = await common._save_credentials_async(
        [{"token": TOKEN_A, "meta": {"chat_id": -100123}}], "telegram_search"
    )

    assert queued == 0
    assert staged.calls == []
    assert redis.set_calls == []
    broker.delay.assert_not_called()


@pytest.mark.asyncio
async def test_results_are_staged_with_source_and_meta(monkeypatch, staged, broker):
    monkeypatch.setattr(common, "redis_client", _Redis())
    results = [
        {"token": TOKEN_A, "meta": {"repo": "owner/a"}, "chat_id": -1001},
        {"token": TOKEN_B, "meta": {"chat_id": -1002}},
        {"token": TOKEN_C},
        {"token": "MANUAL_REVIEW_REQUIRED", "meta": {}},
        {"meta": {"no_token": True}},
    ]

    queued = await common._save_credentials_async(results, "github:code_search")

    assert queued == 3
    assert len(staged.calls) == 1
    assert staged.calls[0] == [
        {"token": TOKEN_A, "source": "github:code_search", "meta": {"repo": "owner/a", "chat_id": -1001}},
        {"token": TOKEN_B, "source": "github:code_search", "meta": {"chat_id": -1002}},
        {"token": TOKEN_C, "source": "github:code_search", "meta": {}},
    ]
    broker.delay.assert_not_called()
    broker.apply_async.assert_not_called()


@pytest.mark.asyncio
async def test_queue_failure_returns_partial_count_and_does_not_raise(monkeypatch, staged, caplog):
    monkeypatch.setattr(common, "redis_client", _Redis())

    def fail_batch_then_drop_one(entries):
        if len(entries) > 1:
            raise RuntimeError("postgrest down")
        if entries[0]["token"] == TOKEN_B:
            raise RuntimeError("row rejected")
        return 1

    staged.behaviour["fn"] = fail_batch_then_drop_one
    results = [{"token": TOKEN_A, "meta": {}}, {"token": TOKEN_B, "meta": {}}, {"token": TOKEN_C, "meta": {}}]

    queued = await common._save_credentials_async(results, "pastebin")

    assert queued == 2
    assert len(staged.calls) == 4  # one failed batch + one retry per candidate
    assert "postgrest down" in caplog.text
    assert TOKEN_A not in caplog.text and TOKEN_B not in caplog.text


@pytest.mark.asyncio
async def test_no_plaintext_token_reaches_broker(monkeypatch, staged, broker):
    monkeypatch.setattr(common, "redis_client", _Redis())
    results = [{"token": TOKEN_A, "meta": {}}, {"token": TOKEN_B, "meta": {}}]

    await common._save_credentials_async(results, "exa")

    assert staged.calls[0][0]["token"] == TOKEN_A
    broker.delay.assert_not_called()
    broker.apply_async.assert_not_called()
