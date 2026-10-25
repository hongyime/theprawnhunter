"""HTTP contract tests for the (disabled-by-default) outpost endpoints.

The real :mod:`app.services.validation_queue_srv` runs against an in-memory
PostgREST double, so these tests exercise the full path: encrypted rows in,
decrypted candidates out, and ``complete`` transitions guarded by the lease.
No live Supabase or network is used.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.core.security import security
from app.services import validation_queue_srv

AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}
TOKEN_A = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TOKEN_B = "2234567890:BBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ── PostgREST double ─────────────────────────────────────────────────────────
def _matches(row: dict, filters: list[tuple]) -> bool:
    for entry in filters:
        kind = entry[0]
        if kind == "eq":
            if row.get(entry[1]) != entry[2]:
                return False
        elif kind == "in":
            if row.get(entry[1]) not in entry[2]:
                return False
        elif kind == "or":
            matched = False
            for part in entry[1].split(","):
                column, _, rest = part.partition(".")
                if rest == "is.null":
                    matched = row.get(column) is None
                elif rest.startswith("lt."):
                    value = row.get(column)
                    matched = value is not None and str(value) < rest[3:]
                else:  # pragma: no cover - guard against fake drift
                    raise AssertionError(f"unsupported or-filter part: {part}")
                if matched:
                    break
            if not matched:
                return False
        else:  # pragma: no cover - guard against fake drift
            raise AssertionError(f"unsupported filter: {entry}")
    return True


class _FakeQuery:
    def __init__(self, store: _FakeStore):
        self._store = store
        self._op: str | None = None
        self._payload: dict | None = None
        self._filters: list[tuple] = []
        self._orders: list[tuple[str, bool]] = []
        self._limit: int | None = None

    def select(self, *_columns: str):
        if self._op is None:
            self._op = "select"
        return self

    def update(self, payload: dict):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def eq(self, column: str, value):
        self._filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values):
        self._filters.append(("in", column, list(values)))
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

    def execute(self):
        return self._store.run(self)


class _FakeStore:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = [copy.deepcopy(row) for row in (rows or [])]
        self.statements: list[str] = []

    def run(self, query: _FakeQuery) -> SimpleNamespace:
        self.statements.append(query._op or "unknown")
        if query._op == "select":
            rows = [row for row in self.rows if _matches(row, query._filters)]
            for column, desc in reversed(query._orders):
                rows.sort(
                    key=lambda row: (row.get(column) is None, row.get(column)),
                    reverse=desc,
                )
            if query._limit is not None:
                rows = rows[: query._limit]
            return SimpleNamespace(data=[copy.deepcopy(row) for row in rows])
        if query._op == "update":
            matched = [row for row in self.rows if _matches(row, query._filters)]
            for row in matched:
                row.update(copy.deepcopy(query._payload or {}))
            return SimpleNamespace(data=[copy.deepcopy(row) for row in matched])
        raise AssertionError(f"unsupported operation: {query._op}")


class _FakeClient:
    def __init__(self, store: _FakeStore):
        self.store = store

    def table(self, name: str) -> _FakeQuery:
        assert name == "validation_queue"
        return _FakeQuery(self.store)


def _row(
    row_id: str,
    *,
    token: str = TOKEN_A,
    source: str = "github",
    meta: dict | None = None,
    priority: int = 10,
    status: str = "pending",
    lease_id: str | None = None,
    expires_at: datetime | None = None,
    attempts: int = 0,
) -> dict:
    return {
        "id": row_id,
        "status": status,
        "attempts": attempts,
        "priority": priority,
        "source": source,
        "meta": meta if meta is not None else {"repo": f"acme/{row_id}"},
        "lease_id": lease_id,
        "lease_expires_at": None if expires_at is None else _iso_z(expires_at),
        "token_ciphertext": security.encrypt(token),
        "created_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def fake_db():
    return _FakeClient(_FakeStore())


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "OUTPOST_ENABLED", True)
    monkeypatch.setattr(settings, "OUTPOST_LEASE_SIZE", 25)
    monkeypatch.setattr(settings, "OUTPOST_LEASE_SECONDS", 300)
    monkeypatch.setattr(settings, "OUTPOST_MAX_ACTIVE", 3)


@pytest.fixture
def complete_spy(monkeypatch):
    calls: list[tuple] = []
    real_complete = validation_queue_srv.complete

    async def spy(lease_id, results, **kwargs):
        calls.append((lease_id, list(results)))
        return await real_complete(lease_id, results, **kwargs)

    monkeypatch.setattr(validation_queue_srv, "complete", spy)
    return calls


def _post_result(client, payload, headers=AUTH):
    return client.post("/monitor/outposts/result", headers=headers, json=payload)


# ── Lease ────────────────────────────────────────────────────────────────────
def test_lease_disabled_is_403_and_never_touches_the_queue(client, monkeypatch):
    monkeypatch.setattr(settings, "OUTPOST_ENABLED", False)
    fake_db = MagicMock()

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = client.post("/monitor/outposts/lease", headers=AUTH)

    assert response.status_code == 403
    assert response.json() == {"detail": "outposts disabled"}
    fake_db.table.assert_not_called()


def test_lease_requires_monitor_key(client, enabled):
    assert client.post("/monitor/outposts/lease").status_code in (401, 403)
    bad = client.post("/monitor/outposts/lease", headers={"X-Monitor-Key": "wrong"})
    assert bad.status_code in (401, 403)


def test_lease_returns_decrypted_priority_ordered_candidates(client, enabled, fake_db):
    fake_db.store.rows = [
        _row("row-low", token=TOKEN_A, priority=10),
        _row("row-high", token=TOKEN_B, priority=90, source="gitlab"),
    ]

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = client.post("/monitor/outposts/lease", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"lease_id", "expires_at", "candidates"}
    assert [c["queue_row_id"] for c in body["candidates"]] == ["row-high", "row-low"]
    assert set(body["candidates"][0]) == {"queue_row_id", "token", "source", "meta"}
    assert body["candidates"][0]["token"] == TOKEN_B
    assert body["candidates"][1]["token"] == TOKEN_A
    assert body["candidates"][0]["meta"] == {"repo": "acme/row-high"}
    # Plaintext leaves the encrypted-at-rest form behind and is never echoed.
    assert "token_ciphertext" not in response.text
    assert all(row["token_ciphertext"] for row in fake_db.store.rows)

    # The service hands the whole batch one shared lease id.
    claimed = [row["lease_id"] for row in fake_db.store.rows]
    assert claimed and set(claimed) == {body["lease_id"]}
    expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert expires_at > datetime.now(UTC)


def test_lease_honours_max_active_with_429(client, enabled, fake_db, monkeypatch):
    monkeypatch.setattr(settings, "OUTPOST_MAX_ACTIVE", 1)
    fake_db.store.rows = [
        _row(
            "row-live",
            status="leased",
            lease_id="lease-live",
            expires_at=datetime.now(UTC) + timedelta(seconds=120),
        ),
        _row("row-pending"),
    ]

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = client.post("/monitor/outposts/lease", headers=AUTH)

    assert response.status_code == 429
    assert "cap" in response.json()["detail"]
    pending = next(row for row in fake_db.store.rows if row["id"] == "row-pending")
    assert pending["status"] == "pending"
    assert pending["lease_id"] is None


def test_lease_empty_queue_is_a_clean_empty_batch(client, enabled, fake_db):
    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = client.post("/monitor/outposts/lease", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"lease_id": None, "expires_at": None, "candidates": []}


# ── Result ───────────────────────────────────────────────────────────────────
def test_result_disabled_is_403_and_never_touches_the_queue(client, monkeypatch):
    monkeypatch.setattr(settings, "OUTPOST_ENABLED", False)
    fake_db = MagicMock()

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = _post_result(
            client, {"lease_id": "lease-1", "queue_row_id": "row-1", "ok": True}
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "outposts disabled"}
    fake_db.table.assert_not_called()


def test_result_requires_monitor_key(client, enabled):
    payload = {"lease_id": "lease-1", "queue_row_id": "row-1", "ok": True}
    assert _post_result(client, payload, headers={}).status_code in (401, 403)
    assert _post_result(client, payload, headers={"X-Monitor-Key": "wrong"}).status_code in (
        401,
        403,
    )


def test_result_unknown_lease_is_409_with_no_state_change(client, enabled, fake_db, complete_spy):
    fake_db.store.rows = [_row("row-1")]

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = _post_result(
            client, {"lease_id": "lease-does-not-exist", "queue_row_id": "row-1", "ok": True}
        )

    print(f"unknown lease -> {response.status_code} {response.json()}")
    assert response.status_code == 409
    assert response.json()["detail"] == "unknown or inactive lease"
    assert complete_spy == []
    row = fake_db.store.rows[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 0


def test_result_unknown_queue_row_is_409(client, enabled, fake_db, complete_spy):
    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = _post_result(
            client, {"lease_id": "lease-1", "queue_row_id": "missing", "ok": True}
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "unknown lease or queue row"
    assert complete_spy == []


def test_result_expired_lease_is_409_with_no_state_change(client, enabled, fake_db, complete_spy):
    fake_db.store.rows = [
        _row(
            "row-1",
            status="leased",
            lease_id="lease-old",
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
    ]

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = _post_result(
            client, {"lease_id": "lease-old", "queue_row_id": "row-1", "ok": True}
        )

    print(f"expired lease -> {response.status_code} {response.json()}")
    assert response.status_code == 409
    assert "expired" in response.json()["detail"]
    assert complete_spy == []
    row = fake_db.store.rows[0]
    assert row["status"] == "leased"
    assert row["attempts"] == 0


def test_result_for_another_live_lease_is_409(client, enabled, fake_db, complete_spy):
    fake_db.store.rows = [
        _row(
            "row-1",
            status="leased",
            lease_id="lease-owner",
            expires_at=datetime.now(UTC) + timedelta(seconds=120),
        )
    ]

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        response = _post_result(
            client, {"lease_id": "lease-forged", "queue_row_id": "row-1", "ok": True}
        )

    assert response.status_code == 409
    assert complete_spy == []
    assert fake_db.store.rows[0]["status"] == "leased"


def test_result_completes_then_replay_is_a_200_noop(client, enabled, fake_db, complete_spy):
    fake_db.store.rows = [
        _row(
            "row-1",
            status="leased",
            lease_id="lease-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=120),
        )
    ]
    payload = {
        "lease_id": "lease-1",
        "queue_row_id": "row-1",
        "ok": True,
        "http_status": 200,
        "reason": "ok",
    }

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        first = _post_result(client, payload)
        second = _post_result(client, payload)

    print(f"first result -> {first.status_code} {first.json()}")
    print(f"duplicate result -> {second.status_code} {second.json()}")
    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(complete_spy) == 1
    row = fake_db.store.rows[0]
    assert row["status"] == "done"
    assert row["attempts"] == 1
    assert row["lease_id"] is None


def test_failed_result_retry_replay_does_not_double_increment(
    client, enabled, fake_db, complete_spy
):
    fake_db.store.rows = [
        _row(
            "row-1",
            status="leased",
            lease_id="lease-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=120),
        )
    ]
    payload = {"lease_id": "lease-1", "queue_row_id": "row-1", "ok": False, "reason": "401"}

    with patch("app.api.routers._monitor.outposts.db", fake_db):
        first = _post_result(client, payload)
        second = _post_result(client, payload)

    assert first.json()["status"] == "completed"
    assert second.json()["status"] == "duplicate"
    assert len(complete_spy) == 1
    row = fake_db.store.rows[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
