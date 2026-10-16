"""Source-ID regressions using real resolvers, without fake databases or clients."""

import pytest

from app.services._bot_listener.commands import _resolve_attachment_source_chat_id
from app.services.archive_source import archive_message_id, honeypot_source_chat_id
from app.services.broadcaster_srv import BroadcasterService
from app.services.honeypot_srv import extract_honeypot_message
from app.services.user_agent_srv import UserAgentService


def captured_attachment():
    record = extract_honeypot_message({"update_id": 901, "message": {
        "message_id": 42, "chat": {"id": -100123},
        "document": {"file_id": "fixture", "file_name": "fixture.txt"},
    }}, "fixture")
    assert record is not None
    record["chat_id"] = -100999
    record["discovered_credentials"] = {"chat_id": -100888, "meta": {
        "bot_username": "WrongSourceFixtureBot",
    }}
    return record


@pytest.mark.asyncio
async def test_archive_prefers_original_chat_over_credential_and_top_level_chat():
    record = captured_attachment()

    resolved = await BroadcasterService()._resolve_chat_id(record)

    assert resolved == -100123
    assert record["telegram_msg_id"] == -901


def test_archive_uses_original_message_id_not_negative_update_id():
    record = captured_attachment()

    assert archive_message_id(record) == 42
    assert archive_message_id(record) != record["telegram_msg_id"]


@pytest.mark.parametrize("invalid", [None, 0, -42, True, "42"])
def test_missing_or_invalid_original_message_id_is_not_guessed(invalid):
    record = captured_attachment()
    record["file_meta"]["message_id"] = invalid

    assert archive_message_id(record) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, 0, True, "@WrongSourceFixtureBot"])
async def test_missing_honeypot_chat_never_falls_back_to_credential_chat(invalid):
    record = captured_attachment()
    record["file_meta"]["chat_id"] = invalid

    assert honeypot_source_chat_id(record) is None
    assert await BroadcasterService()._resolve_chat_id(record) is None
    assert await _resolve_attachment_source_chat_id(record) is None


def test_existing_scraped_message_id_is_unchanged():
    assert archive_message_id({"telegram_msg_id": 57, "file_meta": {"message_id": 999}}) == 57


@pytest.mark.asyncio
async def test_real_archive_entry_rejects_negative_id_before_session_or_file_lookup():
    result = await UserAgentService().archive_media_transiently(
        -100123, -901, target_chat_id=-100999, exact_source=True
    )

    assert result.ok is False
    assert result.code == "invalid_source"


@pytest.mark.asyncio
async def test_getfile_prefers_original_nested_source_chat():
    record = captured_attachment()

    resolved = await _resolve_attachment_source_chat_id(record)

    assert resolved == -100123
