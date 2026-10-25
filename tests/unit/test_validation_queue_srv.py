"""Unit tests for the validation priority queue service.

All tests use an in-memory PostgREST double: no live Supabase connection. The
double applies filters at ``execute()`` time (matching Postgres' guarded-UPDATE
semantics), which is what makes the concurrency assertion meaningful.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.core.security import security
from app.services.validation_queue_srv import (
    complete,
    enqueue,
    lease,
    release_expired,
    score_candidate,
)

TOKEN_A = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TOKEN_B = "2234567890:BBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TOKEN_C = "3234567890:CCxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


# ── PostgREST double ─────────────────────────────────────────────────────────
def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _matches(row: dict[str, Any], filters: list[tuple]) -> bool:
    for entry in filters:
        kind = entry[0]
        if kind == "eq":
            if row.get(entry[1]) != entry[2]:
                return False
        elif kind == "in":
            if row.get(entry[1]) not in entry[2]:
                return False
        elif kind == "is_null":
            if row.get(entry[1]) is not None:
                return False
        elif kind == "lt":
            value = row.get(entry[1])
            if value is None or not str(value) < str(entry[2]):
                return False
        elif kind == "or":
            if not _or_matches(row, entry[1]):
                return False
        else:  # pragma: no cover - guard against silent fake drift
            raise AssertionError(f"unsupported filter: {entry}")
    return True


def _or_matches(row: dict[str, Any], spec: str) -> bool:
    for part in spec.split(","):
        column, _, rest = part.partition(".")
        if rest == "is.null":
            matched = row.get(column) is None
        elif rest.startswith("lt."):
            value = row.get(column)
            matched = value is not None and str(value) < rest[3:]
        else:  # pragma: no cover - guard against silent fake drift
            raise AssertionError(f"unsupported or-filter part: {part}")
        if matched:
            return True
    return False


class _FakeQuery:
    def __init__(self, store: _FakeStore):
        self._store = store
        self._op: str | None = None
        self._payload: Any = None
        self._on_conflict: str = ""
        self._ignore_duplicates = False
        self._filters: list[tuple] = []
        self._orders: list[tuple[str, bool]] = []
        self._limit: int | None = None

    # operation selectors
    def select(self, *_columns: str):
        if self._op is None:
            self._op = "select"
        return self

    def update(self, payload: dict[str, Any]):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def upsert(
        self,
        payload: Any,
        *,
        on_conflict: str = "",
        ignore_duplicates: bool = False,
        default_to_null: bool = True,
    ):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        return self

    # filters / shaping
    def eq(self, column: str, value: Any):
        self._filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values: Any):
        self._filters.append(("in", column, list(values)))
        return self

    def is_(self, column: str, value: Any):
        assert value == "null"
        self._filters.append(("is_null", column))
        return self

    def lt(self, column: str, value: Any):
        self._filters.append(("lt", column, value))
        return self

    def or_(self, filters: str, reference_table: str | None = None):
        self._filters.append(("or", filters))
        return self

    def order(self, column: str, desc: bool = False):
        self._orders.append((column, desc))
        return self

    def limit(self, value: int):
        self._limit = value
        return self

    def execute(self) -> SimpleNamespace:
        return self._store.run(self)


class _FakeStore:
    """Singleton in-memory table enforcing statement-level atomicity."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, select_barrier=None):
        self.rows: list[dict[str, Any]] = [dict(row) for row in (rows or [])]
        self.lock = threading.Lock()
        self.select_barrier = select_barrier
        self._sequence = 0
        self.statements: list[tuple[str, list[tuple]]] = []
        for row in self.rows:
            self._defaults(row)

    def _defaults(self, row: dict[str, Any]) -> None:
        self._sequence += 1
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("status", "pending")
        row.setdefault("attempts", 0)
        row.setdefault("priority", 0)
        row.setdefault("source", None)
        row.setdefault("meta", {})
        row.setdefault("lease_id", None)
        row.setdefault("lease_expires_at", None)
        row.setdefault("token_ciphertext", "")
        row.setdefault("token_hash", hashlib.sha256(str(row["id"]).encode()).hexdigest())
        row.setdefault(
            "created_at",
            (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self._sequence)).isoformat(),
        )

    def run(self, query: _FakeQuery) -> SimpleNamespace:
        # Force concurrent lease() callers to select before they claim, so the
        # test exercises the claim race rather than sequential execution.
        if query._op == "select" and self.select_barrier is not None:
            self.select_barrier.wait(timeout=5)
        with self.lock:
            self.statements.append((query._op or "unknown", list(query._filters)))
            if query._op == "select":
                return self._select(query)
            if query._op == "update":
                return self._update(query)
            if query._op == "upsert":
                return self._upsert(query)
            raise AssertionError(f"unsupported operation: {query._op}")

    def _select(self, query: _FakeQuery) -> SimpleNamespace:
        rows = [row for row in self.rows if _matches(row, query._filters)]
        for column, desc in reversed(query._orders):
            rows.sort(key=lambda row: (row.get(column) is None, row.get(column)), reverse=desc)
        if query._limit is not None:
            rows = rows[: query._limit]
        return SimpleNamespace(data=[copy.deepcopy(row) for row in rows])

    def _update(self, query: _FakeQuery) -> SimpleNamespace:
        matched = [row for row in self.rows if _matches(row, query._filters)]
        for row in matched:
            row.update(copy.deepcopy(query._payload))
        return SimpleNamespace(data=[copy.deepcopy(row) for row in matched])

    def _upsert(self, query: _FakeQuery) -> SimpleNamespace:
        incoming = query._payload if isinstance(query._payload, list) else [query._payload]
        inserted: list[dict[str, Any]] = []
        seen = {row.get(query._on_conflict) for row in self.rows} if query._on_conflict else set()
        for item in incoming:
            row = copy.deepcopy(dict(item))
            self._defaults(row)
            key = row.get(query._on_conflict) if query._on_conflict else row.get("id")
            if query._on_conflict and key in seen:
                if query._ignore_duplicates:
                    continue
                raise AssertionError("duplicate insert without ignore_duplicates")
            seen.add(key)
            self.rows.append(row)
            inserted.append(copy.deepcopy(row))
        return SimpleNamespace(data=inserted)


class _FakeClient:
    def __init__(self, store: _FakeStore):
        self.store = store

    def table(self, name: str) -> _FakeQuery:
        assert name == "validation_queue"
        return _FakeQuery(self.store)


def _client(rows: list[dict[str, Any]] | None = None, select_barrier=None):
    return _FakeClient(_FakeStore(rows, select_barrier=select_barrier))


def _row(
    token: str,
    *,
    source: str = "github",
    meta: dict[str, Any] | None = None,
    priority: int | None = None,
    status: str = "pending",
    attempts: int = 0,
    lease_id: str | None = None,
    lease_expires_at: str | None = None,
) -> dict[str, Any]:
    return {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "token_ciphertext": security.encrypt(token),
        "source": source,
        "meta": meta or {},
        "priority": score_candidate(source, token, meta) if priority is None else priority,
        "status": status,
        "attempts": attempts,
        "lease_id": lease_id,
        "lease_expires_at": lease_expires_at,
    }


# ── Scoring ──────────────────────────────────────────────────────────────────
def test_score_is_deterministic_and_int():
    first = score_candidate("github", TOKEN_A, None)
    second = score_candidate("github", TOKEN_A, None)
    assert first == second
    assert isinstance(first, int)
    assert score_candidate("github", TOKEN_A, {}) == first
    # Source labels may carry a provider sub-source (github:code_search).
    assert score_candidate("GITHUB:code_search", TOKEN_A, {}) == first


def test_score_orders_source_trust_and_meta_monotonically():
    base = score_candidate("github", TOKEN_A, {})
    assert score_candidate("npm", TOKEN_A, {}) > score_candidate("pastebin", TOKEN_A, {})
    assert base > score_candidate("pastebin", TOKEN_A, {})
    assert score_candidate("unknown_novel_scanner", TOKEN_A, {}) < base

    assert score_candidate("github", TOKEN_A, {"repo": "owner/repo"}) > base
    assert score_candidate("github", TOKEN_A, {"webhook_url": "https://x"}) > base
    assert score_candidate("github", TOKEN_A, {"chat_id": -100123}) > base

    one = score_candidate("github", TOKEN_A, {"attempts": 1})
    two = score_candidate("github", TOKEN_A, {"attempts": 2})
    assert one < base
    assert two < one
    # Garbage attempt values degrade to zero rather than raising.
    assert score_candidate("github", TOKEN_A, {"attempts": "n/a"}) == base


# ── Enqueue ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_enqueue_encrypts_hashes_and_skips_duplicates(monkeypatch):
    # The operator .env may enable PLAINTEXT_TOKEN_MODE; pin the encrypted path
    # so this test actually proves ciphertext-at-rest behaviour.
    monkeypatch.setattr(settings, "PLAINTEXT_TOKEN_MODE", False)
    client = _client()
    entries = [
        {
            "token": TOKEN_A,
            "source": "github:code_search",
            "meta": {"repo": "owner/repo", "token": "must-not-persist"},
        }
    ]

    assert await enqueue(entries, client=client) == 1
    row = client.store.rows[0]

    assert row["token_hash"] == hashlib.sha256(TOKEN_A.encode()).hexdigest()
    assert row["token_ciphertext"] != TOKEN_A
    assert security.decrypt(row["token_ciphertext"]) == TOKEN_A
    assert row["status"] == "pending"
    assert row["priority"] == score_candidate("github:code_search", TOKEN_A, {"repo": "owner/repo"})
    # Plaintext never reaches meta, and forbidden keys are stripped.
    assert "token" not in row["meta"]
    assert TOKEN_A not in json.dumps(row)

    # Same token again: unique token_hash means skip, not raise.
    assert await enqueue(entries, client=client) == 0
    assert len(client.store.rows) == 1


# ── Leasing ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_concurrent_leases_return_disjoint_sets():
    # Invariant pinned: lease() claims rows with a guarded UPDATE filtered on
    # status='pending'. Both callers select the same candidates behind a
    # barrier; only the caller that wins the row lock receives each row. A
    # non-atomic read-then-update implementation would return overlapping rows
    # here and fail the disjoint assertion.
    rows = [
        _row(TOKEN_A, priority=30),
        _row(TOKEN_B, priority=20),
        _row(TOKEN_C, priority=10),
    ]
    client = _client(rows, select_barrier=threading.Barrier(2))

    first, second = await asyncio.gather(
        lease(2, 60, client=client),
        lease(2, 60, client=client),
    )

    first_ids = {item["queue_row_id"] for item in first}
    second_ids = {item["queue_row_id"] for item in second}
    assert first_ids.isdisjoint(second_ids)

    # Exactly the rows the database transitioned to 'leased' were handed out,
    # each at most once. A losing lease may return fewer rows (or none), which
    # is correct: another caller already owns them.
    leased_rows = {row["id"] for row in client.store.rows if row["status"] == "leased"}
    assert first_ids | second_ids == leased_rows
    assert len(first_ids) + len(second_ids) == len(leased_rows)
    assert leased_rows

    plaintext = {
        row["id"]: token
        for row, token in zip(client.store.rows, (TOKEN_A, TOKEN_B, TOKEN_C), strict=True)
    }
    for item in [*first, *second]:
        assert item["token"] == plaintext[item["queue_row_id"]]
        assert item["lease_id"]
        assert (
            next(row for row in client.store.rows if row["id"] == item["queue_row_id"])["status"]
            == "leased"
        )


# ── Expiry ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_release_expired_returns_rows_to_pending_without_touching_attempts():
    now = datetime.now(UTC)
    expired = _row(TOKEN_A, status="leased", lease_id=str(uuid.uuid4()), attempts=2)
    expired["lease_expires_at"] = _iso_z(now - timedelta(seconds=1))
    live = _row(TOKEN_B, status="leased", lease_id=str(uuid.uuid4()), attempts=1)
    live["lease_expires_at"] = _iso_z(now + timedelta(seconds=300))
    pending = _row(TOKEN_C)

    client = _client([expired, live, pending])
    assert await release_expired(client=client) == 1

    by_token = {row["token_hash"]: row for row in client.store.rows}
    released = by_token[expired["token_hash"]]
    assert released["status"] == "pending"
    assert released["lease_id"] is None
    assert released["lease_expires_at"] is None
    assert released["attempts"] == 2
    assert by_token[live["token_hash"]]["status"] == "leased"


# ── Completion ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_completion_is_idempotent_and_does_not_double_increment():
    client = _client([_row(TOKEN_A, priority=5)])
    leased = await lease(1, 60, client=client)
    assert len(leased) == 1
    item = leased[0]

    assert (
        await complete(
            item["lease_id"],
            [{"queue_row_id": item["queue_row_id"], "success": True}],
            client=client,
        )
        == 1
    )
    row = client.store.rows[0]
    assert row["status"] == "done"
    assert row["attempts"] == 1
    assert row["lease_id"] is None

    # Replaying the same lease is a no-op.
    assert (
        await complete(
            item["lease_id"],
            [{"queue_row_id": item["queue_row_id"], "success": True}],
            client=client,
        )
        == 0
    )
    assert client.store.rows[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_failure_retries_then_drops_at_max_attempts(monkeypatch):
    monkeypatch.setattr(settings, "VALIDATION_QUEUE_MAX_ATTEMPTS", 3)
    max_attempts = 3

    # Below the ceiling: failure returns the row to pending for retry.
    retry_client = _client([_row(TOKEN_A, priority=1)])
    leased = await lease(1, 60, client=retry_client)
    assert (
        await complete(
            leased[0]["lease_id"],
            [{"queue_row_id": leased[0]["queue_row_id"], "success": False}],
            client=retry_client,
        )
        == 1
    )
    retried = retry_client.store.rows[0]
    assert retried["status"] == "pending"
    assert retried["attempts"] == 1

    # At the ceiling: failure is terminal and the row is dropped.
    drop_client = _client([_row(TOKEN_B, priority=1, attempts=max_attempts - 1)])
    leased = await lease(1, 60, client=drop_client)
    assert (
        await complete(
            leased[0]["lease_id"],
            [{"queue_row_id": leased[0]["queue_row_id"], "success": False}],
            client=drop_client,
        )
        == 1
    )
    dropped = drop_client.store.rows[0]
    assert dropped["status"] == "dropped"
    assert dropped["attempts"] == max_attempts
    assert dropped["lease_id"] is None
