"""Regression coverage for attaching the complete honeypot update to its topic."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class StrictQuery:
    """Small PostgREST fake that only permits select before filter methods."""

    def __init__(self, table_name):
        self.table_name = table_name
        self.operation = None
        self.update_payload = None
        self.filters = []
        self.filters_started = False

    def select(self, *_args, **_kwargs):
        if self.filters_started:
            raise AssertionError("select() must be called before filters")
        self.operation = "select"
        return self

    def update(self, payload):
        self.operation = "update"
        self.update_payload = payload
        return self

    def eq(self, column, value):
        self.filters_started = True
        self.filters.append((column, value))
        return self

    @property
    def not_(self):
        return self

    def gte(self, column, value):
        self.filters_started = True
        self.filters.append((column, value))
        return self

    def lt(self, column, value):
        self.filters_started = True
        self.filters.append((column, value))
        return self

    def is_(self, column, value):
        self.filters_started = True
        self.filters.append((f"is:{column}", value))
        return self

    def in_(self, column, values):
        self.filters_started = True
        self.filters.append((f"in:{column}", values))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def or_(self, value):
        self.filters_started = True
        self.filters.append(("or", value))
        return self

    def limit(self, _count):
        return self


class StrictFakeDb:
    def table(self, table_name):
        return StrictQuery(table_name)


def test_supabase_fake_rejects_select_after_filter():
    query = StrictQuery("exfiltrated_messages")
    query.update({"broadcast_claimed_at": "claim"}).eq("id", "message-1")

    with pytest.raises(AssertionError, match=r"select\(\) must be called before filters"):
        query.select("id")


@pytest.mark.asyncio
async def test_real_evidence_claim_check_uses_datetime(monkeypatch):
    from app.workers.tasks.flows import honeypot as honeypot_tasks

    monkeypatch.setattr(honeypot_tasks, "db", StrictFakeDb())
    monkeypatch.setattr(
        honeypot_tasks,
        "async_execute",
        AsyncMock(return_value=SimpleNamespace(data=[{"id": "message-1"}])),
    )

    valid = await honeypot_tasks._is_honeypot_evidence_claim_valid("message-1", "claim-token")

    assert valid is True


@pytest.mark.asyncio
async def test_summary_update_persists_pending_evidence_with_default_rows(monkeypatch):
    from app.workers.tasks.flows import broadcast as broadcast_tasks

    queries = []

    class FakeDb:
        def table(self, table_name):
            query = StrictQuery(table_name)
            queries.append(query)
            return query

    async def fake_async_execute(query):
        return SimpleNamespace(data=[{"id": "message-1"}])

    monkeypatch.setattr(broadcast_tasks, "db", FakeDb())
    monkeypatch.setattr(broadcast_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(broadcast_tasks, "_can_use_broadcast_reliability_columns", lambda: True)
    monkeypatch.setattr(broadcast_tasks, "_set_broadcast_reliability_columns_available", lambda _value: None)

    updated = await broadcast_tasks._update_message_broadcast_success(
        "message-1",
        broadcast_message_id=9002,
        claim_token="claim-token",
        additional_payload={"file_meta": {"source": "bot_api", "honeypot_evidence_file_status": "pending"}},
    )

    assert updated is True
    assert len(queries) == 1
    assert queries[0].update_payload["file_meta"] == {
        "source": "bot_api",
        "honeypot_evidence_file_status": "pending",
    }
    assert queries[0].update_payload["broadcast_message_id"] == 9002
    assert queries[0].filters == [("id", "message-1"), ("broadcast_claimed_at", "claim-token")]


@pytest.mark.asyncio
async def test_honeypot_sweep_requeues_pending_evidence(monkeypatch):
    from app.workers.tasks import flow_tasks

    pending_message = {"credential_id": "cred-1", "telegram_msg_id": -101}
    queries = []

    class FakeDb:
        def table(self, table_name):
            query = StrictQuery(table_name)
            queries.append(query)
            return query

    responses = iter([
        SimpleNamespace(data=[]),
        SimpleNamespace(data=[]),
        SimpleNamespace(data=[]),
        SimpleNamespace(data=[pending_message]),
    ])

    async def fake_async_execute(query):
        assert query.table_name in {"honeypot_updates", "exfiltrated_messages"}
        return next(responses)

    monkeypatch.setattr(flow_tasks, "db", FakeDb())
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(flow_tasks.settings, "HONEYPOT_REDIRECT_MODE", True)
    monkeypatch.setattr(flow_tasks.settings, "HONEYPOT_REDIRECT_AUTHORIZED", True)
    send_task = MagicMock()
    monkeypatch.setattr(flow_tasks.app, "send_task", send_task)

    result = await flow_tasks._honeypot_redirect_sweep_logic()

    assert result == {"status": "ok", "dispatched": 0, "skipped": 0, "pending": 0}
    assert len(queries) == 4
    for query in queries[2:]:
        retry_filter = next(value for key, value in query.filters if key == "or")
        assert retry_filter.startswith("and(or(next_retry_at.is.null,next_retry_at.lte.")
        assert ",or(broadcast_claimed_at.is.null,broadcast_claimed_at.lt." in retry_filter
    send_task.assert_called_once_with(
        "flow.broadcast_honeypot_message",
        kwargs={"credential_id": "cred-1", "telegram_msg_id": -101},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_record_available", [True, False], ids=["raw-retained", "raw-expired"])
async def test_honeypot_broadcast_sends_evidence_once_and_retry_skips_it(
    monkeypatch,
    raw_record_available,
):
    from app.workers.tasks import flow_tasks

    credential_id = "cred-1"
    telegram_msg_id = -101
    full_text = "captured scam text " * 300
    raw_update = {
        "update_id": 101,
        "message": {
            "message_id": 72,
            "from": {
                "id": 234567,
                "first_name": "Alex",
                "last_name": "Example",
                "username": "scammer_handle",
            },
            "chat": {"id": 987654},
            "text": full_text,
        },
    }
    raw_record = {
        "update_id": 101,
        "payload": raw_update,
        "received_at": "2026-10-12T06:07:08+00:00",
    }
    message = {
        "id": "message-1",
        "credential_id": credential_id,
        "telegram_msg_id": telegram_msg_id,
        "sender_name": "scammer_handle",
        "sender_user_id": 234567,
        "content": full_text[:2000],
        "created_at": "2026-10-12T06:07:08+00:00",
        "is_broadcasted": False,
        "file_meta": {"source": "bot_api", "message_id": 72, "chat_id": 987654},
        "discovered_credentials": {
            "meta": {
                "bot_username": "CapturedBot",
                "bot_id": 7654321,
                "topic_id": 456,
            }
        },
    }

    async def fake_async_execute(query):
        if query.table_name == "honeypot_updates":
            assert query.operation == "select"
            assert ("credential_id", credential_id) in query.filters
            assert ("update_id", 101) in query.filters
            return SimpleNamespace(data=[raw_record] if raw_record_available else [])

        if query.operation == "select":
            return SimpleNamespace(data=[message])

        payload = query.update_payload
        if payload.get("broadcast_claimed_at"):
            message["broadcast_claimed_at"] = payload["broadcast_claimed_at"]
        elif "file_meta" in payload:
            message["file_meta"] = payload["file_meta"]
            message["broadcast_claimed_at"] = None
        return SimpleNamespace(data=[{"id": message["id"]}])

    async def mark_summary_success(*_args, **kwargs):
        message["is_broadcasted"] = True
        message["broadcast_message_id"] = 9002
        message["broadcast_claimed_at"] = None
        message["file_meta"] = {
            **message["file_meta"],
            **kwargs["additional_payload"]["file_meta"],
        }
        return True

    broadcaster = SimpleNamespace(
        ensure_topic=AsyncMock(return_value=456),
        send_message=AsyncMock(return_value=9002),
        send_document=AsyncMock(return_value=9003),
    )
    monkeypatch.setattr(flow_tasks, "db", StrictFakeDb())
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(flow_tasks, "get_broadcaster", lambda: broadcaster)
    monkeypatch.setattr(flow_tasks.settings, "MONITOR_GROUP_ID", -100123)
    monkeypatch.setattr(flow_tasks, "_is_broadcast_claim_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(flow_tasks, "_is_honeypot_evidence_claim_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(
        flow_tasks,
        "_update_message_broadcast_success",
        AsyncMock(side_effect=mark_summary_success),
    )

    result = await flow_tasks._broadcast_single_honeypot_message(credential_id, telegram_msg_id)

    assert result == {"status": "sent", "broadcast_message_id": 9002}
    broadcaster.send_message.assert_awaited_once_with(-100123, 456, message)
    broadcaster.send_document.assert_awaited_once()
    sent_document = broadcaster.send_document.await_args.args[2]
    assert broadcaster.send_document.await_args.args[3] == "honeypot-evidence-101.json"
    evidence = json.loads(sent_document.decode("utf-8"))
    assert evidence["message_text"] == (full_text if raw_record_available else message["content"])
    assert evidence["sender"] == {
        "telegram_user_id": 234567,
        "display_name": "Alex Example" if raw_record_available else "scammer_handle",
        "username": "scammer_handle" if raw_record_available else None,
    }
    assert evidence["received_by"] == {
        "credential_id": credential_id,
        "bot_id": 7654321,
        "bot_username": "CapturedBot",
        "received_at": "2026-10-12T06:07:08+00:00",
    }
    assert evidence["message_ids"]["update_id"] == 101
    assert evidence["message_ids"]["honeypot_telegram_msg_id"] == telegram_msg_id
    assert evidence["message_ids"]["telegram_message_id"] == 72
    assert evidence["message_ids"]["chat_id"] == 987654
    assert evidence["raw_telegram_update"] == (raw_update if raw_record_available else None)
    if raw_record_available:
        assert "raw_copy_note" not in evidence
    else:
        assert evidence["raw_copy_note"] == (
            "The raw Telegram update has expired from honeypot_updates retention."
        )
    assert message["file_meta"]["honeypot_evidence_file_status"] == "sent"

    retry_result = await flow_tasks._broadcast_single_honeypot_message(credential_id, telegram_msg_id)

    assert retry_result == {"status": "already_broadcasted"}
    broadcaster.send_message.assert_awaited_once()
    broadcaster.send_document.assert_awaited_once()
