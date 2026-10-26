import pytest

from app.services._user_agent.group_archive import UserAgentGroupArchive


@pytest.mark.asyncio
async def test_topicless_delete_is_refused():
    archive = UserAgentGroupArchive()
    archive.start = lambda: pytest.fail("must not connect when the delete is refused")

    deleted = await archive.delete_old_messages("@group", age_hours=12, topic_id=None)

    assert deleted == 0


@pytest.mark.asyncio
async def test_general_cleanup_is_disabled_by_default(monkeypatch):
    from app.workers.tasks import audit_tasks

    monkeypatch.setattr(
        audit_tasks.settings, "GENERAL_TOPIC_CLEANUP_ENABLED", False, raising=False
    )

    class _Boom:
        async def delete_old_messages(self, *args, **kwargs):
            pytest.fail("cleanup must not run while disabled")

    monkeypatch.setattr("app.services.user_agent_srv.user_agent", _Boom(), raising=False)

    result = await audit_tasks._cleanup_general_topic_async()
    assert "disabled" in result.lower()
