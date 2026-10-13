"""Regression coverage for attaching the complete honeypot update to its topic."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_record_available", [True, False], ids=["raw-retained", "raw-expired"])
async def test_honeypot_broadcast_sends_evidence_once_and_retry_skips_it(monkeypatch, raw_record_available):
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

    class Query:
        def __init__(self, table_name):
            self.table_name = table_name
            self.operation = None
            self.update_payload = None
            self.filters = []

        def select(self, *_args, **_kwargs):
            self.operation = "select"
            return self

        def update(self, payload):
            self.operation = "update"
            self.update_payload = payload
            return self

        def eq(self, column, value):
            self.filters.append((column, value))
            return self

        def or_(self, value):
            self.filters.append(("or", value))
            return self

        def limit(self, _count):
            return self

    class FakeDb:
        def table(self, table_name):
            return Query(table_name)

    async def fake_async_execute(query):
        if query.table_name == "honeypot_updates":
            assert ("credential_id", credential_id) in query.filters
            assert ("update_id", 101) in query.filters
            return SimpleNamespace(data=[raw_record] if raw_record_available else [])

        if query.update_payload is None:
            return SimpleNamespace(data=[message])

        payload = query.update_payload
        if "file_meta" in payload and payload["file_meta"].get("honeypot_evidence_file_status") == "pending":
            message["file_meta"] = payload["file_meta"]
            message["broadcast_claimed_at"] = payload["broadcast_claimed_at"]
        elif payload.get("file_meta", {}).get("honeypot_evidence_file_status") == "sent":
            message["file_meta"] = payload["file_meta"]
            message["broadcast_claimed_at"] = None
        elif payload.get("broadcast_claimed_at"):
            message["broadcast_claimed_at"] = payload["broadcast_claimed_at"]
        return SimpleNamespace(data=[{"id": message["id"]}])

    async def mark_summary_success(*_args, **_kwargs):
        message["is_broadcasted"] = True
        message["broadcast_message_id"] = 9002
        message["broadcast_claimed_at"] = None
        message["file_meta"] = {
            **message["file_meta"],
            "honeypot_evidence_file_status": "pending",
        }
        return True

    broadcaster = SimpleNamespace(
        ensure_topic=AsyncMock(return_value=456),
        send_message=AsyncMock(return_value=9002),
        send_document=AsyncMock(return_value=9003),
    )
    monkeypatch.setattr(flow_tasks, "db", FakeDb())
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(flow_tasks, "get_broadcaster", lambda: broadcaster)
    monkeypatch.setattr(flow_tasks.settings, "MONITOR_GROUP_ID", -100123)
    monkeypatch.setattr(flow_tasks, "_is_broadcast_claim_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(flow_tasks, "_update_message_broadcast_success", AsyncMock(side_effect=mark_summary_success))

    result = await flow_tasks._broadcast_single_honeypot_message(credential_id, telegram_msg_id)

    assert result == {"status": "sent", "broadcast_message_id": 9002}
    broadcaster.send_message.assert_awaited_once_with(-100123, 456, message)
    broadcaster.send_document.assert_awaited_once()
    sent_document = broadcaster.send_document.await_args.args[2]
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
        "received_at": "2026-10-12T06:07:08+00:00" if raw_record_available else None,
    }
    assert evidence["message_ids"]["update_id"] == 101
    assert evidence["message_ids"]["honeypot_telegram_msg_id"] == telegram_msg_id
    assert evidence["message_ids"]["telegram_message_id"] == 72
    assert evidence["message_ids"]["chat_id"] == 987654
    assert evidence["raw_telegram_update"] == (raw_update if raw_record_available else None)
    if raw_record_available:
        assert "raw_copy_note" not in evidence
    else:
        assert evidence["raw_copy_note"] == "The raw Telegram update has expired from honeypot_updates retention."
    assert message["file_meta"]["honeypot_evidence_file_status"] == "sent"

    retry_result = await flow_tasks._broadcast_single_honeypot_message(credential_id, telegram_msg_id)

    assert retry_result == {"status": "already_broadcasted"}
    broadcaster.send_message.assert_awaited_once()
    broadcaster.send_document.assert_awaited_once()
