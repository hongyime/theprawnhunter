"""TODO 21: high-signal alerts go to General, distinct from system logs."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden


class _FakeRedis:
    def __init__(self):
        self.cached = None
        self.setex_calls = []

    def get(self, _key):
        return self.cached

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))


class _FakeBot:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


def _settings(tokens):
    return SimpleNamespace(
        bot_tokens=tokens,
        MONITOR_GROUP_ID=-100123,
        REDIS_URL="redis://localhost:6379/0",
        TELEGRAM_LOG_MIN_INTERVAL_SECONDS=0,
        TELEGRAM_LOG_FAILURE_WARN_INTERVAL_SECONDS=0,
    )


def _patch_redis(monkeypatch, fake_redis):
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(from_url=lambda _url, decode_responses=True: fake_redis),
    )


def _service(monkeypatch, bots, *, topic_id=777):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings(list(bots)))
    service = broadcaster_srv.BroadcasterService()
    service._acquire_system_log_slot = lambda: True
    service._wait_for_rate_limit = AsyncMock()
    service._get_bot_instance = lambda token: bots[token]
    service.ensure_topic = AsyncMock(return_value=topic_id)
    return service


@pytest.mark.asyncio
async def test_send_alert_targets_general_and_rotates_identities(monkeypatch):
    bots = {
        "123:ABC": _FakeBot(error=Forbidden("bot was kicked")),
        "456:DEF": _FakeBot(),
    }
    service = _service(monkeypatch, bots)

    result = await service.send_alert("watchlist hit")

    assert result is True
    assert bots["123:ABC"].calls and bots["456:DEF"].calls
    delivered = bots["456:DEF"].calls[0]
    assert delivered == {
        "chat_id": -100123,
        "text": "🚨 [Alert]\nwatchlist hit",
    }
    assert "message_thread_id" not in delivered
    assert service._wait_for_rate_limit.await_count == 2


@pytest.mark.asyncio
async def test_send_alert_uses_general_not_the_system_logs_topic(monkeypatch):
    fake_redis = _FakeRedis()
    _patch_redis(monkeypatch, fake_redis)
    bot = _FakeBot()
    service = _service(monkeypatch, {"123:ABC": bot})

    await service.send_log("routine line")
    await service.send_alert("important line")

    assert bot.calls[0]["message_thread_id"] == 777
    assert "message_thread_id" not in bot.calls[1]
    assert "important line" in bot.calls[1]["text"]


@pytest.mark.asyncio
async def test_send_alert_returns_false_when_every_identity_fails(monkeypatch):
    bots = {
        "123:ABC": _FakeBot(error=RuntimeError("first down")),
        "456:DEF": _FakeBot(error=RuntimeError("second down")),
    }
    service = _service(monkeypatch, bots)

    result = await service.send_alert("undeliverable")

    assert result is False
    assert len(bots["123:ABC"].calls) == 1
    assert len(bots["456:DEF"].calls) == 1
