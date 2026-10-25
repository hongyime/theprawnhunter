"""Behaviour of the configurable /starthunter per-user rate limit."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.core.redis_srv import redis_srv
from app.services._bot_listener import login as bot_listener_login


def _make_update_and_context():
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=SimpleNamespace(id=4242),
        message=SimpleNamespace(message_id=7),
    )
    context = SimpleNamespace(
        user_data={},
        bot=SimpleNamespace(username="testbot"),
    )
    return update, context


@pytest.fixture
def sent_replies(monkeypatch):
    sent = []

    async def fake_reply(_update, text, **_kwargs):
        sent.append(text)
        return SimpleNamespace(message_id=100 + len(sent))

    monkeypatch.setattr(bot_listener_login, "_reply_and_delete", fake_reply)
    monkeypatch.setattr(settings, "ALLOW_PUBLIC_STARTHUNTER", True)
    return sent


@pytest.mark.asyncio
async def test_limit_reached_is_rejected(monkeypatch, sent_replies):
    calls = []

    def fake_incr(key, ttl_seconds=None):
        calls.append((key, ttl_seconds))
        return 4

    monkeypatch.setattr(redis_srv, "incr_key", fake_incr)
    monkeypatch.setattr(settings, "STARTHUNTER_RATE_LIMIT_PER_DAY", 3)
    monkeypatch.setattr(settings, "STARTHUNTER_RATE_WINDOW_HOURS", 24)

    update, context = _make_update_and_context()
    result = await bot_listener_login.starthunter(update, context)

    assert result == bot_listener_login.ConversationHandler.END
    assert calls == [("rl:starthunter:4242", 86400)]
    assert "3 per 24h" in sent_replies[-1]
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_attempt_at_limit_is_allowed(monkeypatch, sent_replies):
    monkeypatch.setattr(redis_srv, "incr_key", lambda key, ttl_seconds=None: 3)
    monkeypatch.setattr(settings, "STARTHUNTER_RATE_LIMIT_PER_DAY", 3)
    monkeypatch.setattr(settings, "STARTHUNTER_RATE_WINDOW_HOURS", 24)

    update, context = _make_update_and_context()
    result = await bot_listener_login.starthunter(update, context)

    assert result == bot_listener_login.WAIT_PHONE
    assert "Reply with your phone number" in sent_replies[-1]
    assert context.user_data["bot_messages"] == [7, 101]


@pytest.mark.asyncio
async def test_zero_limit_disables_gate_and_never_raises(monkeypatch, sent_replies):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Redis must not be touched when the limit is disabled")

    monkeypatch.setattr(redis_srv, "incr_key", fail_if_called)
    monkeypatch.setattr(settings, "STARTHUNTER_RATE_LIMIT_PER_DAY", 0)

    update, context = _make_update_and_context()
    result = await bot_listener_login.starthunter(update, context)

    assert result == bot_listener_login.WAIT_PHONE
    assert "Reply with your phone number" in sent_replies[-1]


@pytest.mark.asyncio
async def test_window_hours_control_redis_ttl(monkeypatch, sent_replies):
    calls = []

    def fake_incr(key, ttl_seconds=None):
        calls.append((key, ttl_seconds))
        return 1

    monkeypatch.setattr(redis_srv, "incr_key", fake_incr)
    monkeypatch.setattr(settings, "STARTHUNTER_RATE_LIMIT_PER_DAY", 5)
    monkeypatch.setattr(settings, "STARTHUNTER_RATE_WINDOW_HOURS", 6)

    update, context = _make_update_and_context()
    result = await bot_listener_login.starthunter(update, context)

    assert result == bot_listener_login.WAIT_PHONE
    assert calls == [("rl:starthunter:4242", 21600)]


def test_negative_limit_rejected_but_zero_allowed():
    with pytest.raises(ValidationError):
        Settings(STARTHUNTER_RATE_LIMIT_PER_DAY=-1)

    with pytest.raises(ValidationError):
        Settings(STARTHUNTER_RATE_WINDOW_HOURS=-1)

    assert Settings(STARTHUNTER_RATE_LIMIT_PER_DAY=0).STARTHUNTER_RATE_LIMIT_PER_DAY == 0
