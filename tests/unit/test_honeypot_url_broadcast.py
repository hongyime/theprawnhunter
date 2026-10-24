"""Real URL extraction and broadcast, replacing only Telegram HTTP replies."""

import itertools
import json
from copy import deepcopy
from urllib.parse import parse_qs

import httpx
import pytest
from telegram import Bot
from telegram.request import HTTPXRequest

from app.services.broadcaster_srv import BroadcasterService
from app.services.honeypot_srv import extract_honeypot_message
from app.services.honeypot_urls import format_link_caption


@pytest.mark.asyncio
@pytest.mark.parametrize(("text_field", "entity_field"), [
    ("text", "entities"), ("caption", "caption_entities"),
])
async def test_real_extractor_and_broadcast_preserve_hidden_and_plain_urls(
    text_field: str, entity_field: str
):
    # Given visible text, a hidden destination, and duplicate URL sources.
    visible = "https://visible.invalid/path"
    hidden = "https://hidden.invalid/landing?x=1#section"
    text = f"\U0001f600 Open offer {visible} {visible}"
    hidden_entity = {"type": "text_link", "offset": 3, "length": 10, "url": hidden}
    payload = {"update_id": 101, "message": {
        "message_id": 72, "from": {"first_name": "Fixture"},
        text_field: text,
        entity_field: [hidden_entity, hidden_entity, {
            "type": "url", "offset": 14, "length": len(visible),
        }],
    }}
    original = deepcopy(payload)
    sent_fields: list[dict[str, list[str]]] = []

    def telegram_reply(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        assert request.url.path.endswith("/sendMessage")
        fields = parse_qs(request.content.decode())
        sent_fields.append(fields)
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 9002, "date": 1,
            "chat": {"id": -100123, "type": "supergroup"},
            "text": fields["text"][0],
        }})

    broadcaster = BroadcasterService()
    token = broadcaster.bot_tokens[0]
    broadcaster._pool = [{"type": "bot", "id": token}]
    broadcaster._cycle = itertools.cycle(broadcaster._pool)
    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(telegram_reply)})
    broadcaster._bots[token] = Bot(token, request=request, get_updates_request=request)

    # When the real extractor builds the stored record and the broadcaster sends it.
    record = extract_honeypot_message(payload, "fixture")
    assert record is not None
    try:
        sent_id = await broadcaster.send_message(-100123, 456, record)
    finally:
        await request.shutdown()

    # Then normalized metadata keeps each URL once and preserves the hidden label.
    assert record["file_meta"]["urls"] == [visible, hidden]
    assert record["file_meta"]["text_links"] == [{"text": "Open offer", "url": hidden}]
    assert record["content"] == text
    assert payload == original
    assert sent_id == 9002
    summary = sent_fields[0]["text"][0]
    assert summary.startswith(f"[ID: -101] [From: Fixture]\n{text}")
    links = summary.split("\n\nLinks:\n", 1)[1]
    assert links.count(visible) == 1
    assert links.count(hidden) == 1
    assert len(summary) <= 1024
    assert json.loads(sent_fields[0]["link_preview_options"][0])["is_disabled"] is True


@pytest.mark.parametrize("media_field", ["photo", "document"])
def test_caption_links_are_preserved_without_changing_media_metadata(media_field: str):
    media = [{"file_id": "fixture", "width": 10}] if media_field == "photo" else {
        "file_id": "fixture", "file_name": "fixture.txt",
    }
    payload = {"update_id": 102, "message": {
        "message_id": 73, media_field: media, "caption": "Read notice",
        "caption_entities": [{"type": "text_link", "offset": 0, "length": 11,
                              "url": "https://caption.invalid"}],
    }}

    record = extract_honeypot_message(payload, "fixture")

    assert record is not None
    assert record["media_type"] == media_field
    assert record["content"] == "Read notice"
    assert record["file_meta"]["file_id"] == "fixture"
    assert record["file_meta"]["urls"] == ["https://caption.invalid"]
    assert record["file_meta"]["text_links"] == [
        {"text": "Read notice", "url": "https://caption.invalid"},
    ]


def test_url_entities_capture_bare_domains_and_keep_distinct_hidden_labels():
    text = "example.invalid/path First Second"
    destination = "https://destination.invalid/?a=1#anchor"
    record = extract_honeypot_message({"update_id": 103, "message": {
        "text": text, "entities": [
            {"type": "url", "offset": 0, "length": 20},
            {"type": "text_link", "offset": 21, "length": 5, "url": destination},
            {"type": "text_link", "offset": 27, "length": 6, "url": destination},
        ],
    }}, "fixture")

    assert record is not None
    assert record["file_meta"]["urls"] == ["example.invalid/path", destination]
    assert record["file_meta"]["text_links"] == [
        {"text": "First", "url": destination}, {"text": "Second", "url": destination},
    ]


def test_urls_past_any_length_are_kept_in_full_content():
    url = "https://late.invalid/path"
    text = "x" * 2100 + " " + url
    record = extract_honeypot_message({"update_id": 104, "message": {
        "text": text,
    }}, "fixture")

    assert record is not None
    assert record["content"] == text
    assert record["file_meta"]["urls"] == [url]


def test_duplicate_hidden_and_visible_destination_is_recorded_once():
    url = "https://duplicate.invalid/path"
    record = extract_honeypot_message({"update_id": 105, "message": {
        "text": f"Open {url}", "entities": [
            {"type": "text_link", "offset": 0, "length": 4, "url": url},
            {"type": "url", "offset": 5, "length": len(url)},
        ],
    }}, "fixture")

    assert record is not None
    assert record["file_meta"]["urls"] == [url]
    assert record["file_meta"]["text_links"] == [{"text": "Open", "url": url}]


def test_many_urls_are_all_listed_in_full():
    urls = [f"https://many.invalid/{number}" for number in range(10)]
    summary = format_link_caption("[ID: -106] [From: Fixture]", "\U0001f600" * 2000, urls)

    assert all(url in summary for url in urls)
    assert "+7 more" not in summary


def test_long_url_is_kept_in_full_in_display():
    url = "https://long.invalid/" + "x" * 2000
    record = extract_honeypot_message({"update_id": 107, "message": {"text": url}}, "fixture")
    assert record is not None

    summary = format_link_caption("[ID: -107] [From: Fixture]", record["content"], record["file_meta"]["urls"])

    assert record["file_meta"]["urls"] == [url]
    assert url in summary


def test_plain_url_punctuation_and_balanced_parentheses():
    record = extract_honeypot_message({"update_id": 108, "message": {
        "text": "See (https://one.invalid/path). And https://two.invalid/a_(b)!",
    }}, "fixture")

    assert record is not None
    assert record["file_meta"]["urls"] == ["https://one.invalid/path", "https://two.invalid/a_(b)"]


def test_invalid_entity_offsets_do_not_break_extraction():
    record = extract_honeypot_message({"update_id": 109, "message": {
        "text": "\U0001f600 https://kept.invalid", "entities": [
            {"type": "url", "offset": -1, "length": 4},
            {"type": "url", "offset": 0, "length": 1},
            {"type": "url", "offset": "bad", "length": 4},
            {"type": "url", "offset": 500, "length": 4},
        ],
    }}, "fixture")

    assert record is not None
    assert record["file_meta"]["urls"] == ["https://kept.invalid"]
