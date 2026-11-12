from types import SimpleNamespace

from app.services.user_agent_srv import UserAgentService


def test_forum_anonymous_admin_does_not_need_broadcast_post_permission():
    forum = SimpleNamespace(broadcast=False)
    rights = SimpleNamespace(anonymous=True, post_messages=False)

    assert UserAgentService._can_send_anonymously(forum, rights) is True


def test_broadcast_channel_still_requires_post_permission():
    channel = SimpleNamespace(broadcast=True)
    rights = SimpleNamespace(anonymous=True, post_messages=False)

    assert UserAgentService._can_send_anonymously(channel, rights) is False
