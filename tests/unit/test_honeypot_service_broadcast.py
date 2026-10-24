"""Exercise service-event broadcasts with only Telegram HTTP replies replaced."""

import itertools
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from telegram import Bot
from telegram.request import HTTPXRequest

from app.services.broadcaster_srv import BroadcasterService, BroadcastSendError
from app.services.honeypot_rendering import JsonValue, render_honeypot_broadcast
from app.services.honeypot_srv import extract_honeypot_message


@pytest.mark.asyncio
@pytest.mark.parametrize("update_type", [
    "message", "channel_post", "edited_message", "edited_channel_post",
])
@pytest.mark.parametrize(("event", "expected"), [
    ({"forum_topic_created": {"name": "Fixture topic", "icon_color": 7322096}},
     "Forum topic created"),
    ({"new_chat_members": [{"id": 12, "first_name": "Fixture"}]},
     "New members joined the chat"),
    ({"pinned_message": {"message_id": 71}}, "Message pinned"),
    ({"location": {"latitude": 1.0, "longitude": 2.0}}, "Location shared"),
    ({"contact": {"first_name": "Fixture", "phone_number": "fixture"}}, "Contact shared"),
    ({"poll": {"id": "fixture", "question": "Fixture?"}}, "Poll posted"),
    ({"future_content": {"value": "fixture"}}, "Message received without text or caption"),
])
async def test_service_broadcast_describes_event_without_changing_record(
    update_type: str, event: dict[str, JsonValue], expected: str
):
    # Given a genuine service-update shape and the real extractor/broadcaster.
    payload: dict[str, JsonValue] = {
        "update_id": 101,
        update_type: {
            "message_id": 72,
            "from": {"id": 234567, "first_name": "Fixture"},
            "chat": {"id": -100123},
            **event,
        },
    }
    record = extract_honeypot_message(payload, "fixture")
    assert record is not None
    stored_record = deepcopy(record)
    sent_texts: list[str] = []

    def telegram_reply(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        fields = parse_qs(request.content.decode())
        text = fields["text"][0]
        sent_texts.append(text)
        return httpx.Response(200, json={
            "ok": True,
            "result": {
                "message_id": 9002,
                "date": 1,
                "chat": {"id": -100123, "type": "supergroup"},
                "text": text,
            },
        })

    broadcaster = BroadcasterService()
    token = broadcaster.bot_tokens[0]
    broadcaster._pool = [{"type": "bot", "id": token}]
    broadcaster._cycle = itertools.cycle(broadcaster._pool)
    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(telegram_reply)})
    bot = Bot(token, request=request, get_updates_request=request)
    broadcaster._bots[token] = bot

    # When the real broadcaster sends the extracted record.
    try:
        display_record = render_honeypot_broadcast(record, payload)
        sent_id = await broadcaster.send_message(-100123, 456, display_record)
    finally:
        await request.shutdown()

    # Then Telegram receives the unchanged header plus a useful event description.
    assert sent_id == 9002
    assert sent_texts == [f"[ID: -101] [From: Fixture]\n{expected}"]
    assert record == stored_record
    assert record["content"] == ""


@pytest.mark.parametrize("content", [
    {"text": "Keep this text"},
    {"caption": "Keep this caption", "animation": {"file_id": "fixture"}},
    {"photo": [{"file_id": "fixture"}]},
    {"voice": {"file_id": "fixture"}},
    {"sticker": {"file_id": "fixture"}},
])
def test_rendering_preserves_existing_text_captions_and_media(content: dict[str, JsonValue]):
    payload: dict[str, JsonValue] = {"update_id": 101, "message": {"message_id": 72, **content}}
    record = extract_honeypot_message(payload, "fixture")
    assert record is not None

    rendered = render_honeypot_broadcast(record, payload)

    assert rendered == record
    assert rendered is not record


def test_rendering_leaves_record_unchanged_when_raw_update_is_unavailable():
    record = extract_honeypot_message({"update_id": 101, "message": {"message_id": 72}}, "fixture")
    assert record is not None

    assert render_honeypot_broadcast(record, {}) == record


@pytest.mark.asyncio
async def test_send_message_rejects_empty_text_content():
    broadcaster = BroadcasterService()

    with pytest.raises(BroadcastSendError) as exc_info:
        await broadcaster.send_message(
            -100123,
            456,
            {
                "content": "",
                "sender_name": "Fixture",
                "media_type": "text",
                "telegram_msg_id": 77,
            },
        )

    assert exc_info.value.reason == "empty_content"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_stored_honeypot_broadcast_hydrates_service_description(monkeypatch):
    from app.workers.tasks.flows import broadcast as broadcast_flow

    payload: dict[str, JsonValue] = {
        "update_id": 101,
        "message": {
            "message_id": 72,
            "from": {"id": 234567, "username": "GroupAnonymousBot"},
            "chat": {"id": -100123},
            "new_chat_members": [{"id": 12, "first_name": "Fixture"}],
        },
    }
    record = extract_honeypot_message(payload, "fixture")
    assert record is not None

    async def fake_async_execute(_query):
        return SimpleNamespace(data=[{"payload": payload}])

    monkeypatch.setattr(broadcast_flow, "async_execute", fake_async_execute)

    rendered = await broadcast_flow._render_stored_honeypot_broadcast(record)

    assert rendered["content"] == "New members joined the chat"
    assert rendered is not record
    assert record["content"] == ""
