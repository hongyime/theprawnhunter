"""Unit tests for the heavy ASR task (``heavy.transcribe_media``)."""

import copy
import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from app.workers.tasks.flows import heavy

_MISSING = object()


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


@pytest.fixture(autouse=True)
def _reset_whisper_model(monkeypatch):
    monkeypatch.setattr(heavy, "_WHISPER_MODEL", None)


@pytest.fixture
def store(monkeypatch):
    store = _FakeStore(
        {
            "msg-1": {
                "id": "msg-1",
                "credential_id": "cred-1",
                "media_type": "voice",
                "file_meta": {
                    "file_id": "file-1",
                    "file_size": 4096,
                    "duration": 3,
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


def _install_fake_whisper(
    monkeypatch, *, duration=4.2, language="en", segments=None
):
    calls = {}

    class FakeModel:
        def transcribe(self, source, vad_filter=False):
            calls["vad_filter"] = vad_filter
            calls["audio"] = source.read()
            parts = [" hello", " world "] if segments is None else segments
            return (
                [SimpleNamespace(text=part) for part in parts],
                SimpleNamespace(duration=duration, language=language),
            )

    def factory(*args, **kwargs):
        calls["factory_args"] = args
        calls["factory_kwargs"] = kwargs
        return FakeModel()

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = factory
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return calls


@pytest.mark.asyncio
async def test_transcribe_happy_path_writes_file_meta(monkeypatch, store):
    original_meta = dict(store.rows["msg-1"]["file_meta"])
    broadcaster = _install_broadcaster(monkeypatch, b"fake-ogg-audio")
    calls = _install_fake_whisper(monkeypatch)

    result = await heavy._transcribe_media_logic("msg-1")

    assert result["status"] == "ok"
    assert calls["factory_args"] == ("small",)
    assert calls["factory_kwargs"] == {"device": "cpu", "compute_type": "int8"}
    assert calls["vad_filter"] is True
    assert calls["audio"] == b"fake-ogg-audio"
    assert broadcaster.calls == [(original_meta, "cred-1")]

    file_meta = store.rows["msg-1"]["file_meta"]
    assert file_meta["transcript_text"] == "hello world"
    assert file_meta["transcript_language"] == "en"
    assert file_meta["transcript_model"] == "small-int8"
    assert file_meta["transcript_duration"] == 4.2
    assert "transcript_skipped" not in file_meta
    # Existing keys must survive the merge.
    assert file_meta["file_id"] == "file-1"
    assert file_meta["source"] == "bot_api"


@pytest.mark.asyncio
async def test_transcribe_oversized_declared_media_is_skipped(monkeypatch, store):
    store.rows["msg-1"]["file_meta"]["file_size"] = 25 * 1024 * 1024 + 1
    broadcaster = _Broadcaster(b"unused")
    monkeypatch.setattr(heavy, "get_broadcaster", lambda: broadcaster)
    _install_fake_whisper(monkeypatch)

    result = await heavy._transcribe_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "too_large"}
    assert store.rows["msg-1"]["file_meta"]["transcript_skipped"] == "too_large"
    assert broadcaster.calls == []


@pytest.mark.asyncio
async def test_transcribe_too_long_declared_media_is_skipped(monkeypatch, store):
    store.rows["msg-1"]["file_meta"]["duration"] = 601
    broadcaster = _Broadcaster(b"unused")
    monkeypatch.setattr(heavy, "get_broadcaster", lambda: broadcaster)
    _install_fake_whisper(monkeypatch)

    result = await heavy._transcribe_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "too_long"}
    assert store.rows["msg-1"]["file_meta"]["transcript_skipped"] == "too_long"
    assert broadcaster.calls == []


@pytest.mark.asyncio
async def test_transcribe_missing_media_is_skipped_without_raising(monkeypatch, store):
    _install_broadcaster(monkeypatch, None)
    _install_fake_whisper(monkeypatch)

    result = await heavy._transcribe_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "missing_media"}
    assert store.rows["msg-1"]["file_meta"]["transcript_skipped"] == "missing_media"


@pytest.mark.asyncio
async def test_transcribe_unsupported_media_type_is_skipped(monkeypatch, store):
    store.rows["msg-1"]["media_type"] = "text"
    broadcaster = _Broadcaster(b"unused")
    monkeypatch.setattr(heavy, "get_broadcaster", lambda: broadcaster)

    result = await heavy._transcribe_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "unsupported_media_type"}
    assert broadcaster.calls == []


@pytest.mark.asyncio
async def test_transcribe_model_unavailable_is_skipped(monkeypatch, store):
    _install_broadcaster(monkeypatch, b"fake-ogg-audio")
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    result = await heavy._transcribe_media_logic("msg-1")

    assert result == {"status": "skipped", "reason": "asr_unavailable"}
    assert store.rows["msg-1"]["file_meta"]["transcript_skipped"] == "asr_unavailable"


@pytest.mark.asyncio
async def test_transcribe_is_idempotent(monkeypatch, store):
    store.rows["msg-1"]["file_meta"]["transcript_text"] = "already done"
    broadcaster = _Broadcaster(b"unused")
    monkeypatch.setattr(heavy, "get_broadcaster", lambda: broadcaster)

    result = await heavy._transcribe_media_logic("msg-1")

    assert result == {"status": "already_transcribed"}
    assert broadcaster.calls == []


def test_module_imports_without_heavy_libraries(monkeypatch):
    old_faster_whisper = sys.modules.get("faster_whisper", _MISSING)
    old_pytesseract = sys.modules.get("pytesseract", _MISSING)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    try:
        reloaded = importlib.reload(heavy)
        assert callable(reloaded.transcribe_media)
        assert callable(reloaded.ocr_media)
        # Import safety: no model may be constructed at import time.
        assert reloaded._WHISPER_MODEL is None
    finally:
        if old_faster_whisper is _MISSING:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old_faster_whisper
        if old_pytesseract is _MISSING:
            sys.modules.pop("pytesseract", None)
        else:
            sys.modules["pytesseract"] = old_pytesseract
        importlib.reload(heavy)
