"""Real extraction, file download and multipart upload; only Telegram HTTP is replaced."""

import json
from copy import deepcopy
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from telegram import Bot
from telegram.error import BadRequest
from telegram.request import HTTPXRequest

from app.core.config import settings
from app.services._user_agent.common import ARCHIVE_TMP_DIR, ARCHIVE_TMP_PREFIX
from app.services.honeypot_srv import extract_honeypot_message
from app.services.note_transfer import (
    NoteKind,
    NoteMediaTooLargeError,
    NoteTransfer,
    relay_note_media,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "suffix", "method"), [
    ("voice", ".ogg", "sendVoice"),
    ("video_note", ".mp4", "sendVideoNote"),
    ("sticker", ".webp", "sendSticker"),
    ("sticker", ".tgs", "sendSticker"),
    ("sticker", ".webm", "sendSticker"),
])
@pytest.mark.parametrize("failure", [None, "upload", "summary"])
async def test_note_files_transfer_content_and_clean_up(kind: NoteKind, suffix: str, method: str, failure):
    # Given real extracted metadata and unique file content supplied by Telegram HTTP.
    content = f"{kind}-{uuid4().hex}".encode()
    record = extract_honeypot_message({"update_id": 101, "message": {
        "message_id": 42, "chat": {"id": -100123},
        "from": {"first_name": "Fixture"},
        kind: {"file_id": "fixture", "file_size": len(content)},
    }}, "fixture")
    assert record is not None
    original = deepcopy(record)
    summary = f"[ID: -101] [From: Fixture]\n\n[{kind} Media Detected]"
    calls: list[str] = []
    uploaded_paths: list[Path] = []

    def telegram_reply(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        endpoint = request.url.path.rsplit("/", 1)[-1]
        calls.append(endpoint)
        if endpoint == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": "fixture", "file_unique_id": "unique",
                "file_size": len(content), "file_path": f"notes/fixture{suffix}",
            }})
        if endpoint == f"fixture{suffix}":
            return httpx.Response(200, content=content)
        if endpoint == method:
            body = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
            )
            files = [part for part in body.iter_parts() if part.get_filename()]
            assert len(files) == 1
            assert files[0].get_payload(decode=True) == content
            filename = files[0].get_filename()
            assert filename is not None
            assert filename.endswith(suffix)
            assert filename.startswith(f"{ARCHIVE_TMP_PREFIX}note_")
            paths = [p for p in Path(ARCHIVE_TMP_DIR).glob(f"{ARCHIVE_TMP_PREFIX}note_*{suffix}")
                     if p.read_bytes() == content]
            assert len(paths) == 1
            uploaded_paths.extend(paths)
            if failure == "upload":
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: fixture"})
            return httpx.Response(200, json={"ok": True, "result": {
                "message_id": 11, "date": 1, "chat": {"id": -100123, "type": "supergroup"},
                kind: {"file_id": "arrived", "file_unique_id": "arrived", "duration": 1,
                       "length": 64, "width": 64, "height": 64, "type": "regular",
                       "is_animated": False, "is_video": False},
            }})
        assert endpoint == "sendMessage"
        fields = parse_qs(request.content.decode())
        assert fields["text"] == [summary]
        assert json.loads(fields["reply_parameters"][0])["message_id"] == 11
        if failure == "summary":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: fixture"})
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 12, "date": 1, "chat": {"id": -100123, "type": "supergroup"},
            "text": summary,
        }})

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(telegram_reply)})
    bot = Bot(settings.bot_tokens[0], request=request, get_updates_request=request)
    transfer = NoteTransfer(kind, "fixture", record["file_meta"]["file_id"], -100123, 456, summary)

    # When the actual relay downloads to disk and uploads through the actual Telegram SDK.
    try:
        if failure:
            with pytest.raises(BadRequest):
                await relay_note_media(bot, bot, transfer)
        else:
            result = await relay_note_media(bot, bot, transfer)
            assert getattr(result.media, kind) is not None
            assert result.summary.message_id == 12
    finally:
        await request.shutdown()

    # Then the file, not merely a label, was uploaded and temporary data was removed.
    assert method in calls
    assert calls[:3] == ["getFile", f"fixture{suffix}", method]
    assert uploaded_paths and all(not path.exists() for path in uploaded_paths)
    assert record == original


@pytest.mark.asyncio
async def test_note_size_limit_stops_transfer_before_download():
    size = settings.MAX_ARCHIVE_SIZE_MB * 1024 * 1024 + 1
    calls: list[str] = []

    def telegram_reply(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        calls.append(endpoint)
        assert endpoint == "getFile"
        return httpx.Response(200, json={"ok": True, "result": {
            "file_id": "fixture", "file_unique_id": "unique", "file_size": size,
            "file_path": "notes/fixture.ogg",
        }})

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(telegram_reply)})
    bot = Bot(settings.bot_tokens[0], request=request, get_updates_request=request)
    try:
        with pytest.raises(NoteMediaTooLargeError):
            await relay_note_media(bot, bot, NoteTransfer("voice", "fixture", "fixture", -100123, 456, "note"))
    finally:
        await request.shutdown()

    assert calls == ["getFile"]
