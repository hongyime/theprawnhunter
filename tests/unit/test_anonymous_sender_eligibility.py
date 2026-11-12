from types import SimpleNamespace

import pytest

from app.core.redis_srv import redis_srv
from app.services.user_agent_srv import UserAgentService


def test_forum_anonymous_admin_does_not_need_broadcast_post_permission():
    forum = SimpleNamespace(broadcast=False)
    rights = SimpleNamespace(anonymous=True, post_messages=False)

    assert UserAgentService._can_send_anonymously(forum, rights) is True


def test_broadcast_channel_still_requires_post_permission():
    channel = SimpleNamespace(broadcast=True)
    rights = SimpleNamespace(anonymous=True, post_messages=False)

    assert UserAgentService._can_send_anonymously(channel, rights) is False


@pytest.mark.asyncio
async def test_eligibility_discovers_sessions_on_a_cache_miss(monkeypatch):
    service = UserAgentService()
    discovered = []

    def discover():
        discovered.append(True)

    monkeypatch.setattr(service, "_discover_sessions", discover)
    monkeypatch.setattr(redis_srv.client, "get", lambda _key: None)
    monkeypatch.setattr(redis_srv.client, "setex", lambda *_args: None)

    assert await service.eligible_anonymous_senders("monitor") == []
    assert discovered == [True]
