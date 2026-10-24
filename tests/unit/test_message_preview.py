"""Real extraction, Telegram broadcast and evidence generation; only HTTP replies are replaced."""

import itertools
import json
from urllib.parse import parse_qs

import httpx
import pytest
from telegram import Bot
from telegram.request import HTTPXRequest

from app.core.config import settings
from app.services.broadcaster_srv import BroadcasterService
from app.services.honeypot_srv import extract_honeypot_message
from app.services.honeypot_urls import format_link_preview
from app.services.message_preview import TextPreview, prepare_stored_content
from app.workers.tasks.flows.honeypot import _honeypot_evidence_document


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["text", "caption"])
async def test_over_limit_text_is_stored_and_sent_in_full(field: str):
    full_text = "BEGIN " + "x" * 3000 + " END-FULL-TEXT"
    payload = {"update_id": 101, "message": {
        "message_id": 42, "from": {"first_name": "Fixture"},
        "chat": {"id": -100123}, field: full_text,
    }}
    record = extract_honeypot_message(payload, "fixture")
    assert record is not None
    sent_text: list[str] = []

    def telegram_reply(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        assert request.url.path.endswith("/sendMessage")
        text = parse_qs(request.content.decode())["text"][0]
        sent_text.append(text)
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 901, "date": 1, "chat": {"id": -100123, "type": "supergroup"},
            "text": text,
        }})

    broadcaster = BroadcasterService()
    token = settings.bot_tokens[0]
    broadcaster._pool = [{"type": "bot", "id": token}]
    broadcaster._cycle = itertools.cycle(broadcaster._pool)
    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(telegram_reply)})
    broadcaster._bots[token] = Bot(token, request=request, get_updates_request=request)

    try:
        await broadcaster.send_message(-100123, 456, record)
    finally:
        await request.shutdown()

    assert record["content"] == full_text
    assert record["file_meta"]["content_truncated"] is False
    assert record["file_meta"]["content_original_length"] == len(full_text)
    assert sent_text[0].startswith("[ID: -101] [From: Fixture]\nBEGIN ")
    assert "END-FULL-TEXT" in sent_text[0]
    assert "Preview only" not in sent_text[0]
    document, filename = _honeypot_evidence_document(record, {
        "update_id": 101, "payload": payload, "received_at": record["created_at"],
    }, {})
    evidence = json.loads(document)
    assert filename == "honeypot-evidence-101.json"
    assert evidence["message_text"] == full_text
    assert evidence["raw_telegram_update"] == payload
    assert set(evidence) == {"message_text", "sender", "received_by", "message_ids", "raw_telegram_update"}


@pytest.mark.parametrize("size", [100, 1100, 2000, 2001, 6000])
def test_storage_keeps_full_text_at_all_sizes(size):
    text = "x" * size
    stored, metadata = prepare_stored_content(text)

    rendered = format_link_preview("[ID: -101] [From: Fixture]", TextPreview(stored), [])

    assert stored == text
    assert metadata["content_truncated"] is False
    assert metadata["content_original_length"] == size
    assert rendered == f"[ID: -101] [From: Fixture]\n{text}"


def test_unicode_preview_lists_all_urls_in_full():
    text = "\U0001f600" * 1500
    stored, metadata = prepare_stored_content(text)
    urls = [f"https://fixture.invalid/{number}" for number in range(8)]

    rendered = format_link_preview("[ID: -101] [From: Fixture]", TextPreview(stored), urls)

    assert metadata["content_truncated"] is False
    assert all(url in rendered for url in urls)
    assert "+5 more" not in rendered


def test_long_url_is_listed_in_full_in_display():
    destination = "https://fixture.invalid/" + "a" * 500

    rendered = format_link_preview("[ID: -101] [From: Fixture]", TextPreview("Open notice"), [destination])

    assert destination in rendered
    assert "Open notice" in rendered


@pytest.mark.parametrize("update_type", ["callback_query", "inline_query"])
def test_query_content_is_stored_in_full(update_type):
    query = {"id": "fixture", "from": {"first_name": "Fixture"},
             "data": "x" * 3000, "query": "x" * 3000}
    prefix = "[Callback Query] " if update_type == "callback_query" else "[Inline Query] "

    record = extract_honeypot_message({"update_id": 102, update_type: query}, "fixture")

    assert record is not None
    assert record["content"] == prefix + "x" * 3000
    assert record["file_meta"]["content_truncated"] is False
    assert record["file_meta"]["content_original_length"] == len(record["content"])


def test_short_new_messages_have_an_explicit_complete_storage_flag():
    record = extract_honeypot_message({"update_id": 103, "message": {"text": "Complete text"}}, "fixture")

    assert record is not None
    assert record["content"] == "Complete text"
    assert record["file_meta"]["content_truncated"] is False
    assert record["file_meta"]["content_original_length"] == len("Complete text")
    assert record["file_meta"]["text_preview_version"] == 2
