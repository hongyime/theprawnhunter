from types import SimpleNamespace

import pytest

from app.services import user_agent_srv
from app.services._scraper.results import ScrapeReason
from app.services._scraper.strategies import UserAgentJoinService
from app.services.user_agent_srv import UserAgentService


class _FakeFloodWaitError(Exception):
    pass


class _FloodingClient:
    def __init__(self):
        self.request_count = 0

    async def get_entity(self, _target):
        return SimpleNamespace(id=12345)

    async def __call__(self, _request):
        self.request_count += 1
        raise _FakeFloodWaitError("rate limited")


@pytest.mark.asyncio
async def test_invite_bot_to_group_does_not_fallback_on_flood_wait(monkeypatch):
    service = UserAgentService()
    client = _FloodingClient()
    handled_errors = []
    disconnected = []

    async def start():
        return True

    async def disconnect():
        disconnected.append(True)

    async def handle_flood_error(exc):
        handled_errors.append(exc)

    monkeypatch.setattr(user_agent_srv.errors, "FloodWaitError", _FakeFloodWaitError)
    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(service, "_disconnect", disconnect)
    monkeypatch.setattr(service, "_handle_flood_error", handle_flood_error)
    service.client = client

    result = await service.invite_bot_to_group("example_bot", -100123)

    assert result is False
    assert client.request_count == 1
    assert len(handled_errors) == 1
    assert disconnected == [True]


def test_terminal_invite_error_detector_matches_too_many_bots():
    assert user_agent_srv._is_terminal_invite_error(Exception("Too many bots in this chat"))


@pytest.mark.asyncio
async def test_user_agent_join_service_classifies_cooldown(monkeypatch):
    service = UserAgentJoinService()

    async def resolve(_token):
        return "example_bot", {"getMe_status": 200}

    class _Redis:
        def is_on_cooldown(self, key):
            return key == "user_agent"

        def get_cooldown_remaining(self, _key):
            return 123

    monkeypatch.setattr(service, "resolve_bot_username", resolve)
    monkeypatch.setattr("app.core.redis_srv.redis_srv", _Redis())

    attempt = await service.invite_discovered_bot("123:ABC", -100123)

    assert attempt.reason == ScrapeReason.USER_AGENT_INVITE_FAILED
    assert attempt.retryable is True
    assert attempt.evidence["cooldown_seconds"] == 123
