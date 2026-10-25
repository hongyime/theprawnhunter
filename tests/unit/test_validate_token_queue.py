"""Queue-aware behaviour of ``validation.validate_token``.

These tests pin the ``queue_row_id`` contract: the encrypted row is
authoritative, the row is closed out once per attempt (done/failure), a missing
row degrades to legacy payload validation with a warning, and the two-argument
call path is untouched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.security import security
from app.services import validation_queue_srv
from app.workers.tasks import validation_tasks

TOKEN = "1234567890:AA" + ("x" * 30)
OTHER_TOKEN = "9876543210:BB" + ("y" * 30)


class _FakeTableQuery:
    def __init__(self, store):
        self._store = store
        self._op: str | None = None
        self._payload: dict | None = None
        self._filters: list[tuple[str, object]] = []

    def select(self, *_columns, **_kwargs):
        self._op = "select"
        return self

    def update(self, payload: dict):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def eq(self, column: str, value):
        self._filters.append((column, value))
        return self

    def execute(self):
        row = self._store.row
        if any(row.get(column) != value for column, value in self._filters):
            return SimpleNamespace(data=[])
        if self._op == "select":
            return SimpleNamespace(data=[dict(row)])
        assert self._op == "update"
        row.update(self._payload or {})
        return SimpleNamespace(data=[dict(row)])


class _FakeQueueClient:
    def __init__(self, row: dict):
        self.store = SimpleNamespace(row=dict(row))

    def table(self, name: str):
        assert name == "validation_queue"
        return _FakeTableQuery(self.store)


def _queue_row(
    *,
    token: str = TOKEN,
    lease_id: str | None = "lease-1",
    status: str = "leased",
    attempts: int = 0,
    source: str = "github",
) -> dict:
    return {
        "id": "row-1",
        "token_ciphertext": security.encrypt(token),
        "source": source,
        "meta": {"repo": "owner/repo"},
        "lease_id": lease_id,
        "status": status,
        "attempts": attempts,
    }


def _install_real_complete(monkeypatch, row: dict):
    client = _FakeQueueClient(row)
    real_complete = validation_queue_srv.complete

    async def complete_with_client(lease_id, results):
        return await real_complete(lease_id, results, client=client)

    monkeypatch.setattr(validation_queue_srv, "complete", complete_with_client)
    return client.store


def _loads(row: dict | None):
    async def load(_queue_row_id):
        return row

    return load


def _core(captured: dict | None = None, result: int = 1):
    async def core(item, source_name):
        if captured is not None:
            captured.update(item=item, source_name=source_name)
        return result

    return core


@pytest.mark.asyncio
async def test_queue_row_id_decrypts_and_completes_done(monkeypatch):
    row = _queue_row()
    store = _install_real_complete(monkeypatch, row)
    captured: dict = {}
    monkeypatch.setattr(validation_tasks, "_load_queue_row", _loads(row))
    monkeypatch.setattr(validation_tasks, "_validate_token_core", _core(captured, 1))

    result = await validation_tasks._validate_token_async({}, "dispatcher", queue_row_id="row-1")

    assert result == 1
    # Empty payload still validates: the row's decrypted token is used.
    assert captured["item"]["token"] == TOKEN
    assert captured["source_name"] == "dispatcher"
    assert store.row["status"] == "done"
    assert store.row["attempts"] == 1
    assert store.row["lease_id"] is None


@pytest.mark.asyncio
async def test_queue_row_token_wins_over_payload_token(monkeypatch):
    row = _queue_row()
    store = _install_real_complete(monkeypatch, row)
    captured: dict = {}
    monkeypatch.setattr(validation_tasks, "_load_queue_row", _loads(row))
    monkeypatch.setattr(validation_tasks, "_validate_token_core", _core(captured, 1))

    await validation_tasks._validate_token_async(
        {"token": OTHER_TOKEN, "meta": {"stale": True}},
        "dispatcher",
        queue_row_id="row-1",
    )

    assert captured["item"]["token"] == TOKEN
    assert store.row["status"] == "done"


@pytest.mark.asyncio
async def test_failure_retries_below_max_attempts(monkeypatch):
    monkeypatch.setattr(settings, "VALIDATION_QUEUE_MAX_ATTEMPTS", 3, raising=False)
    row = _queue_row(attempts=0)
    store = _install_real_complete(monkeypatch, row)
    monkeypatch.setattr(validation_tasks, "_load_queue_row", _loads(row))
    monkeypatch.setattr(validation_tasks, "_validate_token_core", _core(None, 0))

    assert (
        await validation_tasks._validate_token_async({}, "dispatcher", queue_row_id="row-1")
        == 0
    )
    assert store.row["status"] == "pending"
    assert store.row["attempts"] == 1
    assert store.row["lease_id"] is None


@pytest.mark.asyncio
async def test_failure_drops_row_after_max_attempts(monkeypatch):
    monkeypatch.setattr(settings, "VALIDATION_QUEUE_MAX_ATTEMPTS", 3, raising=False)
    row = _queue_row(attempts=2)
    store = _install_real_complete(monkeypatch, row)
    monkeypatch.setattr(validation_tasks, "_load_queue_row", _loads(row))
    monkeypatch.setattr(validation_tasks, "_validate_token_core", _core(None, 0))

    await validation_tasks._validate_token_async({}, "dispatcher", queue_row_id="row-1")

    assert store.row["status"] == "dropped"
    assert store.row["attempts"] == 3
    assert store.row["lease_id"] is None


@pytest.mark.asyncio
async def test_unknown_queue_row_validates_from_payload_and_warns(monkeypatch, caplog):
    store = _install_real_complete(monkeypatch, _queue_row())
    captured: dict = {}
    monkeypatch.setattr(validation_tasks, "_load_queue_row", _loads(None))
    monkeypatch.setattr(validation_tasks, "_validate_token_core", _core(captured, 1))
    caplog.set_level("WARNING", logger="validation.tasks")

    result = await validation_tasks._validate_token_async(
        {"token": TOKEN}, "dispatcher", queue_row_id="deleted-row"
    )

    assert result == 1
    assert captured["item"]["token"] == TOKEN
    assert "not found" in caplog.text
    # Unknown row must not be completed (there is no lease handle).
    assert store.row["status"] == "leased"
    assert store.row["attempts"] == 0


@pytest.mark.asyncio
async def test_not_leased_row_validates_without_completion(monkeypatch):
    row = _queue_row(lease_id=None, status="pending")
    store = _install_real_complete(monkeypatch, row)
    captured: dict = {}
    monkeypatch.setattr(validation_tasks, "_load_queue_row", _loads(row))
    monkeypatch.setattr(validation_tasks, "_validate_token_core", _core(captured, 1))

    result = await validation_tasks._validate_token_async({}, "dispatcher", queue_row_id="row-1")

    assert result == 1
    assert captured["item"]["token"] == TOKEN
    assert store.row["status"] == "pending"
    assert store.row["attempts"] == 0


@pytest.mark.asyncio
async def test_legacy_path_does_not_touch_the_queue(monkeypatch):
    completes: list = []

    async def load(_queue_row_id):
        raise AssertionError("legacy calls must not read validation_queue")

    async def complete(_lease_id, results):
        completes.append(results)
        return 0

    captured: dict = {}
    monkeypatch.setattr(validation_tasks, "_load_queue_row", load)
    monkeypatch.setattr(validation_tasks, "_validate_token_core", _core(captured, 1))
    monkeypatch.setattr(validation_queue_srv, "complete", complete)

    result = await validation_tasks._validate_token_async({"token": TOKEN}, "github")

    assert result == 1
    assert captured["item"]["token"] == TOKEN
    assert captured["source_name"] == "github"
    assert completes == []


def test_sync_wrapper_forwards_optional_queue_row_id(monkeypatch):
    seen: dict = {}

    async def fake_async(item, source_name, queue_row_id=None):
        seen.update(item=item, source_name=source_name, queue_row_id=queue_row_id)
        return 1

    class InlineLoop:
        def run_until_complete(self, coroutine):
            return asyncio.run(coroutine)

    import app.workers.celery_app as celery_app_module

    monkeypatch.setattr(validation_tasks, "_validate_token_async", fake_async)
    monkeypatch.setattr(celery_app_module, "get_worker_loop", lambda: InlineLoop())

    assert (
        validation_tasks.validate_token({"token": TOKEN}, "github", queue_row_id="row-1") == 1
    )
    assert seen == {
        "item": {"token": TOKEN},
        "source_name": "github",
        "queue_row_id": "row-1",
    }
