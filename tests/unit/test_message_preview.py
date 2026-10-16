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
async def test_over_limit_text_has_explicit_preview_and_marked_record(field: str):
    # Given a new over-limit message with a recognisable beginning and end.
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

    # When the real broadcaster sends the actual extracted record.
    try:
        await broadcaster.send_message(-100123, 456, record)
    finally:
        await request.shutdown()

    # Then a reader sees a preview notice, while unchanged evidence retains all text.
    assert record["content"] == full_text[:2000]
    assert record["file_meta"]["content_truncated"] is True
    assert record["file_meta"]["content_original_length"] == len(full_text)
    assert sent_text[0].startswith("[ID: -101] [From: Fixture]\nBEGIN ")
    assert "Preview only" in sent_text[0]
    assert "evidence JSON file" in sent_text[0]
    assert "END-FULL-TEXT" not in sent_text[0]
    assert len(sent_text[0].encode("utf-16-le")) // 2 <= 1024
    document, filename = _honeypot_evidence_document(record, {
        "update_id": 101, "payload": payload, "received_at": record["created_at"],
    }, {})
    evidence = json.loads(document)
    assert filename == "honeypot-evidence-101.json"
    assert evidence["message_text"] == full_text
    assert evidence["raw_telegram_update"] == payload
    assert set(evidence) == {"message_text", "sender", "received_by", "message_ids", "raw_telegram_update"}


@pytest.mark.parametrize(("size", "stored_cut", "preview_cut"), [
    (100, False, False), (1100, False, True), (2000, False, True), (2001, True, True),
])
def test_storage_and_broadcast_cut_limits_are_marked_separately(size, stored_cut, preview_cut):
    text = "x" * size
    stored, metadata = prepare_stored_content(text)

    rendered = format_link_preview("[ID: -101] [From: Fixture]", TextPreview(stored, metadata["content_truncated"]), [])

    assert len(stored) == min(size, 2000)
    assert metadata["content_truncated"] is stored_cut
    assert metadata["content_original_length"] == size
    assert ("Preview only" in rendered) is preview_cut
    assert len(rendered.encode("utf-16-le")) // 2 <= 1024
    if not preview_cut:
        assert rendered == f"[ID: -101] [From: Fixture]\n{text}"


def test_unicode_preview_reserves_space_for_notice_and_links():
    text = "\U0001f600" * 1500
    stored, metadata = prepare_stored_content(text)
    urls = [f"https://fixture.invalid/{number}" for number in range(8)]

    rendered = format_link_preview("[ID: -101] [From: Fixture]", TextPreview(stored), urls)

    assert metadata["content_truncated"] is False
    assert "Preview only" in rendered
    assert "evidence JSON file" in rendered
    assert all(url in rendered for url in urls[:3])
    assert "+5 more" in rendered
    assert len(rendered.encode("utf-16-le")) // 2 <= 1024


def test_shortened_hidden_link_has_explicit_full_evidence_location():
    destination = "https://fixture.invalid/" + "a" * 500

    rendered = format_link_preview("[ID: -101] [From: Fixture]", TextPreview("Open notice"), [destination])

    assert "Links shortened" in rendered
    assert "full links in evidence JSON file" in rendered
    assert "Open notice" in rendered
    assert len(rendered.encode("utf-16-le")) // 2 <= 1024


@pytest.mark.parametrize("update_type", ["callback_query", "inline_query"])
def test_query_content_cuts_are_also_marked(update_type):
    query = {"id": "fixture", "from": {"first_name": "Fixture"},
             "data": "x" * 3000, "query": "x" * 3000}

    record = extract_honeypot_message({"update_id": 102, update_type: query}, "fixture")

    assert record is not None
    assert len(record["content"]) == 2000
    assert record["file_meta"]["content_truncated"] is True
    assert record["file_meta"]["content_original_length"] > 3000


def test_short_new_messages_have_an_explicit_complete_storage_flag():
    record = extract_honeypot_message({"update_id": 103, "message": {"text": "Complete text"}}, "fixture")

    assert record is not None
    assert record["content"] == "Complete text"
    assert record["file_meta"]["content_truncated"] is False
    assert record["file_meta"]["content_original_length"] == len("Complete text")
    assert record["file_meta"]["text_preview_version"] == 1
