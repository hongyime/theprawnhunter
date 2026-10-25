"""Unit tests for the heavy OCR task (``heavy.ocr_media``)."""

import copy
import io
import sys
import types
from types import SimpleNamespace

import pytest

from app.workers.tasks.flows import heavy


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.update_calls = []


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
            return SimpleNamespace(data=[copy.deepcopy(row)] if row else [])
        assert row is not None, "update on missing row"
        row.update(copy.deepcopy(self.payload))
        self.store.update_calls.append(copy.deepcopy(self.payload))
        return SimpleNamespace(data=[copy.deepcopy(row)])


class _FakeDb:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        assert name == "exfiltrated_messages"
        return _FakeQuery(self.store)


class _Broadcaster:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def _download_media_bytes(self, file_meta, credential_id):
        self.calls.append((copy.deepcopy(file_meta), credential_id))
        return self.data


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def store(monkeypatch):
    store = _FakeStore(
        {
            "msg-1": {
                "id": "msg-1",
                "credential_id": "cred-1",
                "media_type": "document",
                "file_meta": {
                    "file_id": "file-1",
                    "file_size": 2048,
                    "mime": "image/png",
                    "file_name": "screenshot.png",
                    "source": "bot_api",
                },
            }
        }
    )
    fake_db = _FakeDb(store)

    async def fake_async_execute(query):
        return query.execute()

    monkeypatch.setattr(heavy, "db", fake_db)
    monkeypatch.setattr(heavy, "async_execute", fake_async_execute)
    return store


def _install_broadcaster(monkeypatch, data):
    broadcaster = _Broadcaster(data)
    monkeypatch.setattr(heavy, "get_broadcaster", lambda: broadcaster)
    return broadcaster


def _install_fake_tesseract(monkeypatch, text=" hello ocr \n"):
    calls = []

    def image_to_string(image, lang=None):
        calls.append({"size": image.size, "lang": lang})
        return text

    module = types.ModuleType("pytesseract")
    module.image_to_string = image_to_string
    monkeypatch.setitem(sys.modules, "pytesseract", module)
    return calls


@pytest.mark.asyncio
async def test_ocr_document_image_happy_path(monkeypatch, store):
    original_meta = dict(store.rows["msg-1"]["file_meta"])
    broadcaster = _install_broadcaster(monkeypatch, _png_bytes())
    calls = _install_fake_tesseract(monkeypatch)

    result = await heavy._ocr_media_logic("msg-1")

    assert result["status"] == "ok"
    assert calls == [{"size": (8, 8), "lang": "eng+chi_sim+rus"}]
    assert broadcaster.calls == [(original_meta, "cred-1")]

    file_meta = store.rows["msg-1"]["file_meta"]
    assert file_meta["ocr_text"] == "hello ocr"
    assert file_meta["ocr_language"] == "eng+chi_sim+rus"
    assert file_meta["ocr_model"] == "tesseract"
    assert "ocr_skipped" not in file_meta
    # Existing keys must survive the merge.
    assert file_meta["file_id"] == "file-1"
    assert file_meta["source"] == "bot_api"


@pytest.mark.asyncio
async def test_ocr_photo_is_accepted_without_mime(monkeypatch, store):
    store.rows["msg-1"]["media_type"] = "photo"
    for key in ("mime", "file_name"):
        store.rows["msg-1"]["file_meta"].pop(key)
    _install_broadcaster(monkeypatch, _png_bytes())
    calls = _install_fake_tesseract(monkeypatch)

    result = await heavy._ocr_media_logic("msg-1")

    assert result["status"] == "ok"
    assert calls[0]["lang"] == "eng+chi_sim+rus"


@pytest.mark.asyncio
async def test_ocr_non_image_document_is_skipped(monkeypatch, store):
    store.rows["msg-1"]["file_meta"].update(
        {"mime": "application/pdf", "file_name": "report.pdf"}
    )
    broadcaster = _Broadcaster(b"unused")
    monkeypatch.setattr(heavy, "get_broadcaster", lambda: broadcaster)
    _install_fake_tesseract(monkeypatch)

    result = await heavy._ocr_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "non_image"}
    assert store.rows["msg-1"]["file_meta"]["ocr_skipped"] == "non_image"
    assert broadcaster.calls == []


@pytest.mark.asyncio
async def test_ocr_oversized_media_is_skipped(monkeypatch, store):
    store.rows["msg-1"]["file_meta"]["file_size"] = 10 * 1024 * 1024 + 1
    broadcaster = _Broadcaster(b"unused")
    monkeypatch.setattr(heavy, "get_broadcaster", lambda: broadcaster)
    _install_fake_tesseract(monkeypatch)

    result = await heavy._ocr_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "too_large"}
    assert store.rows["msg-1"]["file_meta"]["ocr_skipped"] == "too_large"
    assert broadcaster.calls == []


@pytest.mark.asyncio
async def test_ocr_corrupt_image_is_skipped_without_raising(monkeypatch, store):
    _install_broadcaster(monkeypatch, b"this is definitely not an image")
    calls = _install_fake_tesseract(monkeypatch)

    result = await heavy._ocr_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "corrupt_image"}
    assert store.rows["msg-1"]["file_meta"]["ocr_skipped"] == "corrupt_image"
    assert calls == []


@pytest.mark.asyncio
async def test_ocr_missing_media_is_skipped(monkeypatch, store):
    _install_broadcaster(monkeypatch, None)
    _install_fake_tesseract(monkeypatch)

    result = await heavy._ocr_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "missing_media"}
    assert store.rows["msg-1"]["file_meta"]["ocr_skipped"] == "missing_media"


@pytest.mark.asyncio
async def test_ocr_engine_unavailable_is_skipped(monkeypatch, store):
    _install_broadcaster(monkeypatch, _png_bytes())
    monkeypatch.setitem(sys.modules, "pytesseract", None)

    result = await heavy._ocr_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "ocr_unavailable"}
    assert store.rows["msg-1"]["file_meta"]["ocr_skipped"] == "ocr_unavailable"


@pytest.mark.asyncio
async def test_ocr_is_idempotent(monkeypatch, store):
    store.rows["msg-1"]["file_meta"]["ocr_text"] = "already ocr'd"
    broadcaster = _Broadcaster(b"unused")
    monkeypatch.setattr(heavy, "get_broadcaster", lambda: broadcaster)

    result = await heavy._ocr_media_logic("msg-1")

    assert result == {"status": "already_ocred"}
    assert broadcaster.calls == []
