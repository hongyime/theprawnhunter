"""Staging-path behaviour of ``validation.refresh_pending_tokens``.

The refresh sweep must never put a plaintext token on the Celery broker: it
stages candidates through the encrypted ``validation_queue`` and skips rows that
are already pending for the dispatcher.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.core.security import security
from app.services import validation_queue_srv
from app.workers.tasks import validation_tasks

TOKEN = "1234567890:AA" + ("x" * 30)
PENDING_TOKEN = "9876543210:BB" + ("y" * 30)


class _FakeQuery:
    def __init__(self, table_name: str, rows: list[dict]):
        self.table_name = table_name
        self.rows = rows
        self.filters: dict[str, object] = {}

    def select(self, *_args, **_kwargs):
        return self

    def is_(self, *_args, **_kwargs):
        return self

    def gt(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def in_(self, column, values):
        self.filters[column] = list(values)
        return self


class _FakeDB:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.calls: list[_FakeQuery] = []

    def table(self, name: str):
        query = _FakeQuery(name, self.tables.get(name, []))
        self.calls.append(query)
        return query


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, **_kwargs):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


def _install(monkeypatch, *, credentials, queue_rows):
    fake_db = _FakeDB(
        {
            "discovered_credentials": credentials,
            "validation_queue": queue_rows,
        }
    )

    async def fake_execute(query):
        rows = list(query.rows)
        status = query.filters.get("status")
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        hashes = query.filters.get("token_hash")
        if hashes is not None:
            wanted = set(hashes)
            rows = [row for row in rows if row.get("token_hash") in wanted]
        return SimpleNamespace(data=rows)

    redis = _FakeRedis()
    monkeypatch.setattr(validation_tasks, "db", fake_db)
    monkeypatch.setattr(validation_tasks, "async_execute", fake_execute)
    monkeypatch.setattr(validation_tasks, "redis_client", redis)
    return fake_db, redis


def _credential(token: str, *, row_id: str = "cred-1") -> dict:
    return {
        "id": row_id,
        "bot_token": security.encrypt(token),
        "meta": {"seen": row_id},
        "updated_at": "2026-10-25T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_refresh_stages_eligible_tokens_without_broker_payload(monkeypatch):
    _install(
        monkeypatch,
        credentials=[_credential(TOKEN, row_id="cred-1")],
        queue_rows=[],
    )
    staged: list[dict] = []
    broker_payloads: list[dict] = []

    async def fake_enqueue(entries):
        staged.extend(entries)
        return len(entries)

    monkeypatch.setattr(validation_queue_srv, "enqueue", fake_enqueue)
    monkeypatch.setattr(
        validation_tasks.validate_token,
        "apply_async",
        lambda *args, **kwargs: broker_payloads.append({"args": args, "kwargs": kwargs}),
    )

    result = await validation_tasks._refresh_pending_tokens_async()

    assert result == "refreshed:1"
    assert len(staged) == 1
    assert staged[0]["token"] == TOKEN
    assert staged[0]["source"] == "refresh_pending"
    assert staged[0]["meta"] == {"seen": "cred-1"}
    assert broker_payloads == []
    assert TOKEN not in str(broker_payloads)


@pytest.mark.asyncio
async def test_refresh_skips_rows_already_pending_in_queue(monkeypatch):
    _install(
        monkeypatch,
        credentials=[_credential(TOKEN)],
        queue_rows=[
            {
                "token_hash": validation_queue_srv.token_hash(TOKEN),
                "status": "pending",
            }
        ],
    )
    staged: list[dict] = []

    async def fake_enqueue(entries):
        staged.extend(entries)
        return len(entries)

    monkeypatch.setattr(validation_queue_srv, "enqueue", fake_enqueue)

    result = await validation_tasks._refresh_pending_tokens_async()

    assert result == "refreshed:0"
    assert staged == []


@pytest.mark.asyncio
async def test_refresh_queue_write_failure_is_logged_not_raised(monkeypatch, caplog):
    _install(
        monkeypatch,
        credentials=[_credential(TOKEN)],
        queue_rows=[],
    )

    async def failing_enqueue(_entries):
        raise RuntimeError("queue write exploded")

    monkeypatch.setattr(validation_queue_srv, "enqueue", failing_enqueue)

    with caplog.at_level(logging.ERROR, logger="validation.tasks"):
        result = await validation_tasks._refresh_pending_tokens_async()

    assert result == "refreshed:0"
    assert any("staging pending tokens failed" in record.message for record in caplog.records)
