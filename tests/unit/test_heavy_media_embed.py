"""Heavy ASR/OCR tasks must refresh the vector after merging derived text.

The embed hook itself is faked; these tests only pin the trigger point and the
best-effort contract:

* a successful transcript/OCR merge calls the hook with the *merged* file_meta,
  so the payload is ``searchable_media_text(content, file_meta)``;
* ``EMBED_API_URL`` unset means no hook call at all;
* a raising hook never fails the enrichment task or prevents the file_meta write.
"""

from __future__ import annotations

import io
import sys
import types
from types import SimpleNamespace

import pytest

from app.services import embedding_hook
from app.services.media_text import searchable_media_text
from app.workers.tasks.flows import heavy


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows


class _FakeQuery:
    def __init__(self, store):
        self.store = store
        self.op = None
        self.payload = None
        self.key = None

    def select(self, _columns):
        self.op = "select"
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def eq(self, column, value):
        assert column == "id"
        self.key = value
        return self

    def limit(self, _count):
        return self

    def execute(self):
        row = self.store.rows.get(self.key)
        if self.op == "select":
            return SimpleNamespace(data=[dict(row)] if row else [])
        assert row is not None, "update on missing row"
        row.update(self.payload)
        return SimpleNamespace(data=[dict(row)])


class _FakeDb:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        assert name == "exfiltrated_messages"
        return _FakeQuery(self.store)


@pytest.fixture
def store(monkeypatch):
    store = _FakeStore(
        {
            "msg-1": {
                "id": "msg-1",
                "credential_id": "cred-1",
                "media_type": "voice",
                "content": "voice caption",
                "file_meta": {"file_id": "file-1", "file_size": 4096, "duration": 3},
            }
        }
    )

    async def fake_async_execute(query):
        return query.execute()

    monkeypatch.setattr(heavy, "db", _FakeDb(store))
    monkeypatch.setattr(heavy, "async_execute", fake_async_execute)
    return store


def _install_asr(monkeypatch, transcript=" hello world "):
    class FakeModel:
        def transcribe(self, source, vad_filter=False):
            return (
                [SimpleNamespace(text=transcript)],
                SimpleNamespace(duration=4.2, language="en"),
            )

    monkeypatch.setattr(heavy, "_get_whisper_model", lambda: FakeModel())

    async def fake_download(_file_meta, _credential_id):
        return b"fake-audio"

    monkeypatch.setattr(heavy, "_download", fake_download)


def _install_ocr(monkeypatch, text=" hello ocr \n"):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")

    async def fake_download(_file_meta, _credential_id):
        return buffer.getvalue()

    monkeypatch.setattr(heavy, "_download", fake_download)

    module = types.ModuleType("pytesseract")
    module.image_to_string = lambda _image, lang=None: text
    monkeypatch.setitem(sys.modules, "pytesseract", module)


def _capture_hook(monkeypatch, *, raises: Exception | None = None):
    calls = []

    def fake_embed(message_id, content, *, file_meta=None, **_kwargs):
        calls.append({"message_id": message_id, "content": content, "file_meta": file_meta})
        if raises is not None:
            raise raises
        return True

    monkeypatch.setattr(embedding_hook, "embed_message_best_effort", fake_embed)
    return calls


@pytest.mark.asyncio
async def test_asr_success_reembeds_content_plus_transcript(monkeypatch, store):
    monkeypatch.setenv(embedding_hook.EMBED_API_URL_ENV, "http://embed.test")
    _install_asr(monkeypatch)
    calls = _capture_hook(monkeypatch)

    result = await heavy._transcribe_media_logic("msg-1")

    assert result["status"] == "ok"
    assert len(calls) == 1
    call = calls[0]
    assert call["message_id"] == "msg-1"
    assert call["file_meta"]["transcript_text"] == "hello world"
    # Existing keys survive the merge that the hook sees.
    assert call["file_meta"]["file_id"] == "file-1"
    # Payload contract: exactly the shared searchable document.
    assert searchable_media_text(call["content"], call["file_meta"]) == (
        "voice caption\nhello world"
    )
    # And the persisted row is the same merged document.
    assert store.rows["msg-1"]["file_meta"]["transcript_text"] == "hello world"


@pytest.mark.asyncio
async def test_ocr_success_reembeds_content_plus_ocr(monkeypatch, store):
    store.rows["msg-1"]["media_type"] = "photo"
    store.rows["msg-1"]["content"] = "image caption"
    monkeypatch.setenv(embedding_hook.EMBED_API_URL_ENV, "http://embed.test")
    _install_ocr(monkeypatch)
    calls = _capture_hook(monkeypatch)

    result = await heavy._ocr_media_logic("msg-1")

    assert result["status"] == "ok"
    assert len(calls) == 1
    call = calls[0]
    assert call["file_meta"]["ocr_text"] == "hello ocr"
    assert searchable_media_text(call["content"], call["file_meta"]) == (
        "image caption\nhello ocr"
    )
    assert store.rows["msg-1"]["file_meta"]["ocr_text"] == "hello ocr"


@pytest.mark.asyncio
async def test_asr_does_not_call_hook_when_embed_api_is_unset(monkeypatch, store):
    monkeypatch.delenv(embedding_hook.EMBED_API_URL_ENV, raising=False)
    _install_asr(monkeypatch)
    calls = _capture_hook(monkeypatch, raises=AssertionError("hook must not run"))

    result = await heavy._transcribe_media_logic("msg-1")

    assert result["status"] == "ok"
    assert calls == []
    assert store.rows["msg-1"]["file_meta"]["transcript_text"] == "hello world"


@pytest.mark.asyncio
async def test_ocr_does_not_call_hook_when_embed_api_is_unset(monkeypatch, store):
    store.rows["msg-1"]["media_type"] = "photo"
    monkeypatch.delenv(embedding_hook.EMBED_API_URL_ENV, raising=False)
    _install_ocr(monkeypatch)
    calls = _capture_hook(monkeypatch, raises=AssertionError("hook must not run"))

    result = await heavy._ocr_media_logic("msg-1")

    assert result["status"] == "ok"
    assert calls == []
    assert store.rows["msg-1"]["file_meta"]["ocr_text"] == "hello ocr"


@pytest.mark.asyncio
async def test_asr_survives_embed_hook_failure(monkeypatch, store, caplog):
    monkeypatch.setenv(embedding_hook.EMBED_API_URL_ENV, "http://embed.test")
    _install_asr(monkeypatch)
    _capture_hook(monkeypatch, raises=RuntimeError("embed-api down"))

    with caplog.at_level("WARNING"):
        result = await heavy._transcribe_media_logic("msg-1")

    assert result["status"] == "ok"
    assert store.rows["msg-1"]["file_meta"]["transcript_text"] == "hello world"
    assert "embedding refresh skipped for msg=msg-1" in caplog.text
    # Logs carry the id/count, never the transcript contents.
    assert "hello world" not in caplog.text


@pytest.mark.asyncio
async def test_ocr_survives_embed_hook_failure(monkeypatch, store, caplog):
    store.rows["msg-1"]["media_type"] = "photo"
    monkeypatch.setenv(embedding_hook.EMBED_API_URL_ENV, "http://embed.test")
    _install_ocr(monkeypatch)
    _capture_hook(monkeypatch, raises=RuntimeError("embed-api down"))

    with caplog.at_level("WARNING"):
        result = await heavy._ocr_media_logic("msg-1")

    assert result["status"] == "ok"
    assert store.rows["msg-1"]["file_meta"]["ocr_text"] == "hello ocr"
    assert "embedding refresh skipped for msg=msg-1" in caplog.text
    assert "hello ocr" not in caplog.text
