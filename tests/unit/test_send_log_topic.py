"""TODO 20: system logs are delivered to a cached System Logs topic."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _FakeRedis:
    def __init__(self, cached=None):
        self.cached = cached
        self.gets = []
        self.setex_calls = []

    def get(self, key):
        self.gets.append(key)
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


def _settings():
    return SimpleNamespace(
        bot_tokens=["123:ABC"],
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


def _service(monkeypatch, bot, *, topic_id=None, error=None):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings())
    service = broadcaster_srv.BroadcasterService()
    service._acquire_system_log_slot = lambda: True
    service._wait_for_rate_limit = AsyncMock()
    service._get_bot_instance = lambda _token: bot
    if error is not None:
        async def _raise(*_args, **_kwargs):
            raise error

        service.ensure_topic = _raise
    else:
        service.ensure_topic = AsyncMock(return_value=topic_id)
    return service


@pytest.mark.asyncio
async def test_send_log_uses_resolved_topic_and_caches_id(monkeypatch):
    fake_redis = _FakeRedis()
    _patch_redis(monkeypatch, fake_redis)
    bot = _FakeBot()
    service = _service(monkeypatch, bot, topic_id=777)

    sent = await service.send_log("routine chatter")

    assert sent is True
    service.ensure_topic.assert_awaited_once_with(-100123, "System Logs")
    assert bot.calls == [
        {
            "chat_id": -100123,
            "text": "🤖 [System Log]\nroutine chatter",
            "message_thread_id": 777,
        }
    ]
    assert fake_redis.setex_calls == [("topic:system_logs:-100123", 86400, "777")]
    service._wait_for_rate_limit.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_log_uses_cached_topic_without_ensuring(monkeypatch):
    fake_redis = _FakeRedis(cached="888")
    _patch_redis(monkeypatch, fake_redis)
    bot = _FakeBot()
    service = _service(monkeypatch, bot, topic_id=777)

    sent = await service.send_log("cached")

    assert sent is True
    service.ensure_topic.assert_not_awaited()
    assert bot.calls[0]["message_thread_id"] == 888
    assert fake_redis.setex_calls == []


@pytest.mark.asyncio
async def test_send_log_falls_back_to_general_when_topic_creation_fails(monkeypatch, caplog):
    fake_redis = _FakeRedis()
    _patch_redis(monkeypatch, fake_redis)
    bot = _FakeBot()
    service = _service(monkeypatch, bot, error=RuntimeError("topic create exploded"))

    sent = await service.send_log("must still arrive")

    assert sent is True
    assert bot.calls == [
        {
            "chat_id": -100123,
            "text": "🤖 [System Log]\nmust still arrive",
        }
    ]
    assert "message_thread_id" not in bot.calls[0]
    assert fake_redis.setex_calls == []
    assert "falling back to General" in caplog.text
