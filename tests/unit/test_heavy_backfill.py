"""Unit tests for the heavy media-text backfill (``heavy.backfill_media_text``)."""

import copy
from types import SimpleNamespace

import pytest

from app.workers.tasks.flows import heavy


class _FakeStore:
    def __init__(self, rows):
        self.rows = {row["id"]: row for row in rows}
        self.update_calls = []


class _FakeQuery:
    def __init__(self, store):
        self.store = store
        self.op = None
        self.payload = None
        self.key = None
        self._in = None
        self._range = None
        self._orders = []

    def select(self, _columns):
        self.op = "select"
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def in_(self, column, values):
        assert column == "media_type"
        self._in = set(values)
        return self

    def order(self, column):
        self._orders.append(column)
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def eq(self, column, value):
        assert column == "id"
        self.key = value
        return self

    def execute(self):
        if self.op == "select":
            rows = [copy.deepcopy(row) for row in self.store.rows.values()]
            if self._in is not None:
                rows = [row for row in rows if row["media_type"] in self._in]
            rows.sort(key=lambda row: (row.get("created_at"), row.get("id")))
            if self._range is not None:
                start, end = self._range
                rows = rows[start : end + 1]
            return SimpleNamespace(data=rows)

        assert self.op == "update"
        row = self.store.rows[self.key]
        row.update(copy.deepcopy(self.payload))
        self.store.update_calls.append(copy.deepcopy(self.payload))
        return SimpleNamespace(data=[copy.deepcopy(row)])


class _FakeDb:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        assert name == "exfiltrated_messages"
        return _FakeQuery(self.store)


class _FakeTask:
    def __init__(self):
        self.calls = []

    def apply_async(self, args=None, queue=None):
        self.calls.append((tuple(args or ()), queue))


def _row(message_id, media_type, file_meta, created_at):
    return {
        "id": message_id,
        "media_type": media_type,
        "file_meta": dict(file_meta),
        "created_at": created_at,
    }


@pytest.fixture
def store(monkeypatch):
    store = _FakeStore(
        [
            _row(
                "voice-1", "voice", {"file_id": "f1", "source": "bot_api"}, "2026-01-01T00:00:00Z"
            ),
            _row("photo-1", "photo", {"file_id": "f2"}, "2026-01-01T00:00:01Z"),
            _row(
                "doc-image",
                "document",
                {"file_id": "f3", "mime_type": "image/png"},
                "2026-01-01T00:00:02Z",
            ),
            # Excluded: non-image document has no applicable pipeline.
            _row(
                "doc-pdf",
                "document",
                {"file_id": "f4", "mime_type": "application/pdf"},
                "2026-01-01T00:00:03Z",
            ),
            # Excluded: extraction already produced text.
            _row(
                "voice-done",
                "voice",
                {"transcript_text": "already here"},
                "2026-01-01T00:00:04Z",
            ),
            _row(
                "photo-done",
                "photo",
                {"ocr_text": "already here"},
                "2026-01-01T00:00:05Z",
            ),
            # Excluded: corresponding skip marker is present.
            _row(
                "voice-skipped",
                "voice",
                {"transcript_skipped": "too_large"},
                "2026-01-01T00:00:06Z",
            ),
            _row(
                "photo-skipped",
                "photo",
                {"ocr_skipped": "corrupt_image"},
                "2026-01-01T00:00:07Z",
            ),
            # Excluded by the media_type filter.
            _row("text-1", "text", {}, "2026-01-01T00:00:08Z"),
        ]
    )
    fake_db = _FakeDb(store)

    async def fake_async_execute(query):
        return query.execute()

    monkeypatch.setattr(heavy, "db", fake_db)
    monkeypatch.setattr(heavy, "async_execute", fake_async_execute)
    monkeypatch.delenv("HEAVY_BACKFILL_DELAY_SECONDS", raising=False)
    return store


@pytest.fixture
def dispatch(monkeypatch):
    asr = _FakeTask()
    ocr = _FakeTask()
    monkeypatch.setattr(heavy, "transcribe_media", asr)
    monkeypatch.setattr(heavy, "ocr_media", ocr)
    return SimpleNamespace(asr=asr, ocr=ocr)


@pytest.mark.asyncio
async def test_dry_run_selects_without_acting(store, dispatch):
    result = await heavy._backfill_media_text_logic(dry_run=True)

    assert result["status"] == "dry_run"
    assert result["selected"] == 3
    assert result["pending"] == {"asr": 1, "ocr": 2}
    assert result["dispatched"] == {"asr": 0, "ocr": 0}
    assert dispatch.asr.calls == []
    assert dispatch.ocr.calls == []
    assert store.update_calls == []


@pytest.mark.asyncio
async def test_apply_dispatches_at_most_batch_size_oldest_first(monkeypatch, dispatch):
    rows = [
        _row(
            f"voice-{index:03d}",
            "voice",
            {"file_id": f"f{index}"},
            f"2026-02-01T00:00:{index:02d}Z",
        )
        for index in range(60)
    ]
    store = _FakeStore(rows)
    fake_db = _FakeDb(store)

    async def fake_async_execute(query):
        return query.execute()

    monkeypatch.setattr(heavy, "db", fake_db)
    monkeypatch.setattr(heavy, "async_execute", fake_async_execute)
    monkeypatch.setenv("HEAVY_BACKFILL_DELAY_SECONDS", "0")

    result = await heavy._backfill_media_text_logic(dry_run=False)

    assert result["selected"] == heavy.HEAVY_BACKFILL_BATCH_SIZE == 50
    assert result["dispatched"] == {"asr": 50, "ocr": 0}
    assert [call[0] for call in dispatch.asr.calls] == [
        (f"voice-{index:03d}",) for index in range(50)
    ]
    assert all(call[1] == "heavy" for call in dispatch.asr.calls)
    assert len(store.update_calls) == 50
    for row in rows[:50]:
        assert heavy.HEAVY_BACKFILL_MARKER_KEY in store.rows[row["id"]]["file_meta"]
    for row in rows[50:]:
        assert heavy.HEAVY_BACKFILL_MARKER_KEY not in store.rows[row["id"]]["file_meta"]


@pytest.mark.asyncio
async def test_apply_stamps_marker_and_preserves_unrelated_meta(store, dispatch):
    result = await heavy._backfill_media_text_logic(dry_run=False)

    assert result["selected"] == 3
    meta = store.rows["voice-1"]["file_meta"]
    assert meta["source"] == "bot_api"
    assert meta["file_id"] == "f1"
    assert meta[heavy.HEAVY_BACKFILL_MARKER_KEY].endswith("+00:00")

    assert [call[0] for call in dispatch.asr.calls] == [("voice-1",)]
    assert [call[0] for call in dispatch.ocr.calls] == [("photo-1",), ("doc-image",)]


@pytest.mark.asyncio
async def test_second_run_selects_zero(store, dispatch):
    first = await heavy._backfill_media_text_logic(dry_run=False)
    assert first["selected"] == 3
    calls_after_first = (len(dispatch.asr.calls), len(dispatch.ocr.calls))

    second = await heavy._backfill_media_text_logic(dry_run=False)

    assert second["selected"] == 0
    assert second["dispatched"] == {"asr": 0, "ocr": 0}
    assert (len(dispatch.asr.calls), len(dispatch.ocr.calls)) == calls_after_first


@pytest.mark.asyncio
async def test_skipped_media_rows_are_excluded(monkeypatch, dispatch):
    store = _FakeStore(
        [
            _row("v", "voice", {"transcript_skipped": "too_long"}, "2026-03-01T00:00:00Z"),
            _row("p", "photo", {"ocr_skipped": "too_large"}, "2026-03-01T00:00:01Z"),
            _row("d", "document", {"mime_type": "application/pdf"}, "2026-03-01T00:00:02Z"),
        ]
    )
    fake_db = _FakeDb(store)

    async def fake_async_execute(query):
        return query.execute()

    monkeypatch.setattr(heavy, "db", fake_db)
    monkeypatch.setattr(heavy, "async_execute", fake_async_execute)

    result = await heavy._backfill_media_text_logic(dry_run=False)

    assert result["selected"] == 0
    assert dispatch.asr.calls == []
    assert dispatch.ocr.calls == []
    assert store.update_calls == []


@pytest.mark.asyncio
async def test_rate_limit_sleep_between_items(monkeypatch, dispatch):
    store = _FakeStore(
        [
            _row("v1", "voice", {"file_id": "a"}, "2026-04-01T00:00:00Z"),
            _row("v2", "voice", {"file_id": "b"}, "2026-04-01T00:00:01Z"),
            _row("v3", "voice", {"file_id": "c"}, "2026-04-01T00:00:02Z"),
        ]
    )
    fake_db = _FakeDb(store)

    async def fake_async_execute(query):
        return query.execute()

    sleeps = []
    monkeypatch.setattr(heavy, "db", fake_db)
    monkeypatch.setattr(heavy, "async_execute", fake_async_execute)
    monkeypatch.setattr(heavy.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setenv("HEAVY_BACKFILL_DELAY_SECONDS", "1.5")

    result = await heavy._backfill_media_text_logic(dry_run=False)

    assert result["selected"] == 3
    assert len(dispatch.asr.calls) == 3
    assert sleeps == [1.5, 1.5]


def test_backfill_delay_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("HEAVY_BACKFILL_DELAY_SECONDS", raising=False)
    assert heavy._backfill_delay_seconds() == 2.0

    monkeypatch.setenv("HEAVY_BACKFILL_DELAY_SECONDS", "not-a-number")
    assert heavy._backfill_delay_seconds() == 2.0

    monkeypatch.setenv("HEAVY_BACKFILL_DELAY_SECONDS", "-5")
    assert heavy._backfill_delay_seconds() == 0.0
