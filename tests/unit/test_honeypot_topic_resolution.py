"""Regression coverage for routing honeypot broadcasts to their bot topic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_honeypot_broadcast_revalidates_saved_topic_id(monkeypatch):
    from app.workers.tasks import flow_tasks

    credential_id = "cred-1"
    telegram_msg_id = -101
    stale_topic_id = 1
    correct_topic_id = 9001
    message = {
        "id": "message-1",
        "credential_id": credential_id,
        "telegram_msg_id": telegram_msg_id,
        "is_broadcasted": False,
        "file_meta": {"source": "bot_api"},
        "discovered_credentials": {
            "meta": {
                "bot_username": "ExampleBot",
                "bot_id": 123456,
                "topic_id": stale_topic_id,
            }
        },
    }

    class Query:
        def __init__(self, table_name):
            self.table_name = table_name
            self.update_payload = None
            self.filters_started = False

        def select(self, *_args, **_kwargs):
            if self.filters_started:
                raise AssertionError("select() must be called before filters")
            return self

        def update(self, payload):
            self.update_payload = payload
            return self

        def eq(self, *_args, **_kwargs):
            self.filters_started = True
            return self

        def or_(self, *_args, **_kwargs):
            self.filters_started = True
            return self

        def limit(self, *_args, **_kwargs):
            return self

    class FakeDb:
        def __init__(self):
            self.queries = []

        def table(self, table_name):
            query = Query(table_name)
            self.queries.append(query)
            return query

    fake_db = FakeDb()
    async_execute = AsyncMock(side_effect=[
        SimpleNamespace(data=[message]),
        SimpleNamespace(data=[{"id": message["id"]}]),
        SimpleNamespace(data=[{"meta": {**message["discovered_credentials"]["meta"], "keep": "value"}}]),
        SimpleNamespace(data=[{"id": credential_id}]),
        SimpleNamespace(data=[{"id": message["id"]}]),
        SimpleNamespace(data=[]),
        SimpleNamespace(data=[{"id": message["id"]}]),
    ])
    broadcaster = SimpleNamespace(
        ensure_topic=AsyncMock(return_value=correct_topic_id),
        send_message=AsyncMock(return_value=9002),
        send_document=AsyncMock(return_value=9003),
    )

    monkeypatch.setattr(flow_tasks, "db", fake_db)
    monkeypatch.setattr(flow_tasks, "async_execute", async_execute)
    monkeypatch.setattr(flow_tasks, "get_broadcaster", lambda: broadcaster)
    monkeypatch.setattr(flow_tasks.settings, "MONITOR_GROUP_ID", -100123)
    monkeypatch.setattr(flow_tasks, "_is_broadcast_claim_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(flow_tasks, "_is_honeypot_evidence_claim_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(flow_tasks, "_update_message_broadcast_success", AsyncMock(return_value=True))

    result = await flow_tasks._broadcast_single_honeypot_message(credential_id, telegram_msg_id)

    assert result == {"status": "sent", "broadcast_message_id": 9002}
    broadcaster.ensure_topic.assert_awaited_once_with(
        -100123,
        "@ExampleBot / 123456",
        preferred_topic_id=stale_topic_id,
    )
    broadcaster.send_message.assert_awaited_once_with(-100123, correct_topic_id, message)
    broadcaster.send_document.assert_awaited_once_with(
        -100123,
        correct_topic_id,
        broadcaster.send_document.await_args.args[2],
        "honeypot-evidence-101.json",
    )
    assert fake_db.queries[3].table_name == "discovered_credentials"
    assert fake_db.queries[3].update_payload == {
        "meta": {
            "bot_username": "ExampleBot",
            "bot_id": 123456,
            "topic_id": correct_topic_id,
            "keep": "value",
        }
    }


@pytest.mark.asyncio
async def test_ensure_topic_keeps_a_valid_saved_topic(monkeypatch):
    from app.services import broadcaster_srv

    service = broadcaster_srv.BroadcasterService.__new__(broadcaster_srv.BroadcasterService)
    topic_name = "@ExampleBot / 123456"
    with patch.object(broadcaster_srv.user_agent, "get_topic_title", new_callable=AsyncMock, return_value=topic_name) as get_title, \
         patch.object(broadcaster_srv.user_agent, "find_topic_id", new_callable=AsyncMock) as find_topic:
        result = await service.ensure_topic(-100123, topic_name, preferred_topic_id=9001)

    assert result == 9001
    get_title.assert_awaited_once_with(-100123, 9001)
    find_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_topic_replaces_a_saved_id_that_points_to_logs(monkeypatch):
    from app.services import broadcaster_srv

    service = broadcaster_srv.BroadcasterService.__new__(broadcaster_srv.BroadcasterService)
    topic_name = "@ExampleBot / 123456"
    with patch.object(broadcaster_srv.user_agent, "get_topic_title", new_callable=AsyncMock, return_value="Logs") as get_title, \
         patch.object(broadcaster_srv.user_agent, "find_topic_id", new_callable=AsyncMock, return_value=9001) as find_topic:
        result = await service.ensure_topic(-100123, topic_name, preferred_topic_id=1)

    assert result == 9001
    get_title.assert_awaited_once_with(-100123, 1)
    find_topic.assert_awaited_once_with(-100123, topic_name)


@pytest.mark.asyncio
async def test_ensure_topic_does_not_create_when_topic_list_lookup_fails():
    from app.services import broadcaster_srv

    service = broadcaster_srv.BroadcasterService.__new__(broadcaster_srv.BroadcasterService)
    with patch.object(
        broadcaster_srv.user_agent,
        "find_topic_id",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Telegram topic list unavailable"),
    ) as find_topic:
        with pytest.raises(RuntimeError, match="Could not resolve existing topic"):
            await service.ensure_topic(-100123, "@ExampleBot / 123456")

    find_topic.assert_awaited_once_with(-100123, "@ExampleBot / 123456")


@pytest.mark.asyncio
async def test_find_topic_id_uses_telethon_messages_request():
    from telethon.tl.functions.messages import GetForumTopicsRequest
    from app.services.user_agent_srv import UserAgentService

    service = UserAgentService()
    topic_name = "@ExampleBot / 123456"

    class FakeClient:
        async def get_entity(self, _target):
            return "group-entity"

        async def __call__(self, request):
            assert isinstance(request, GetForumTopicsRequest)
            return SimpleNamespace(topics=[
                SimpleNamespace(title="Logs", id=1),
                SimpleNamespace(title=topic_name, id=9001),
            ])

    service.client = FakeClient()
    with patch.object(service, "start", new_callable=AsyncMock, return_value=True), \
         patch.object(service, "_disconnect", new_callable=AsyncMock):
        topic_id = await service.find_topic_id(-100123, topic_name)

    assert topic_id == 9001
