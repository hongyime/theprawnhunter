"""Unit tests for the /monitor/search FTS + ILIKE fallback behaviour."""

import asyncio
import logging
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError

from app.api.routers._monitor import search as search_mod
from app.schemas.models import SearchMatchOut, SearchOut

AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}
CREDENTIAL_ID = UUID("11111111-1111-1111-1111-111111111111")
HIT_ID = "22222222-2222-2222-2222-222222222222"

FTS_ROWS = [
    {
        "id": HIT_ID,
        "credential_id": str(CREDENTIAL_ID),
        "sender_key": "user_abc12345",
        "sender_pseudonym": "user_def67890",
        "snippet": "<b>bitcoin</b> wallet dump",
        "media_type": "text",
        "created_at": "2026-10-25T00:00:00+00:00",
    }
]

LEGACY_ROWS = [
    {
        "id": HIT_ID,
        "credential_id": str(CREDENTIAL_ID),
        "telegram_msg_id": 42,
        "sender_name": "alice",
        "content": "bitcoin wallet dump",
        "media_type": "text",
        "is_broadcasted": False,
        "created_at": "2026-10-25T00:00:00+00:00",
        "broadcasted_at": None,
    }
]


class _FakeTableQuery:
    def __init__(self, rows, error):
        self._rows = rows
        self._error = error
        self.calls = []
        self.limit_value = None

    def select(self, columns):
        self.calls.append(("select", columns))
        return self

    def or_(self, expression):
        self.calls.append(("or_", expression))
        return self

    def order(self, column, desc=False):
        self.calls.append(("order", column, desc))
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def eq(self, column, value):
        self.calls.append(("eq", column, value))
        return self

    def neq(self, column, value):
        self.calls.append(("neq", column, value))
        return self

    def gte(self, column, value):
        self.calls.append(("gte", column, value))
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=self._rows)


class _FakeRpcBuilder:
    def __init__(self, rows, error):
        self._rows = rows
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=self._rows)


class _FakeDB:
    def __init__(self, rpc_rows=None, rpc_error=None, table_rows=None, table_error=None):
        self.rpc_calls = []
        self.table_calls = []
        self._rpc_rows = rpc_rows
        self._rpc_error = rpc_error
        self._table_rows = table_rows or []
        self._table_error = table_error
        self.last_table_query = None

    def rpc(self, name, params=None):
        self.rpc_calls.append((name, params))
        return _FakeRpcBuilder(self._rpc_rows, self._rpc_error)

    def table(self, name):
        self.table_calls.append(name)
        self.last_table_query = _FakeTableQuery(self._table_rows, self._table_error)
        return self.last_table_query


def _call(**kwargs):
    return asyncio.run(search_mod.search_messages(**kwargs))


def _use_db(monkeypatch, fake_db):
    monkeypatch.setattr(search_mod, "db", fake_db)
    monkeypatch.setattr(search_mod, "_fts_fallback_warned", False)
    monkeypatch.setattr(search_mod, "_ilike_degraded_warned", False)


def _statement_timeout_error() -> APIError:
    return APIError(
        {
            "message": "canceling statement due to statement timeout",
            "code": "57014",
            "details": None,
            "hint": None,
        }
    )


def test_fts_path_passes_clamped_limit_and_credential_id(monkeypatch):
    fake_db = _FakeDB(rpc_rows=FTS_ROWS)
    _use_db(monkeypatch, fake_db)

    body = _call(q="bitcoin", limit=999, credential_id=CREDENTIAL_ID)

    assert fake_db.rpc_calls == [
        (
            "search_evidence",
            {
                "q": "bitcoin",
                "credential_id": str(CREDENTIAL_ID),
                "lim": 200,
            },
        )
    ]
    assert fake_db.table_calls == []
    assert body["match_count"] == 1
    assert body["limit"] == 200
    assert body["matches"][0]["snippet"] == "<b>bitcoin</b> wallet dump"


@pytest.mark.parametrize(("requested", "expected"), [(0, 1), (-5, 1), (50, 50), (999, 200)])
def test_limit_is_clamped_to_one_and_two_hundred(monkeypatch, requested, expected):
    fake_db = _FakeDB(rpc_rows=[])
    _use_db(monkeypatch, fake_db)

    body = _call(q="bitcoin", limit=requested)

    assert fake_db.rpc_calls[0][1]["lim"] == expected
    assert body["limit"] == expected


def test_two_char_query_is_rejected(monkeypatch):
    fake_db = _FakeDB()
    _use_db(monkeypatch, fake_db)

    with pytest.raises(HTTPException) as excinfo:
        _call(q="ab", limit=50, credential_id=CREDENTIAL_ID)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "query must be at least 3 chars"
    assert fake_db.rpc_calls == []
    assert fake_db.table_calls == []


def test_three_char_query_uses_fts(monkeypatch):
    fake_db = _FakeDB(rpc_rows=FTS_ROWS)
    _use_db(monkeypatch, fake_db)

    body = _call(q="abc", limit=50, credential_id=CREDENTIAL_ID)

    assert fake_db.rpc_calls == [
        (
            "search_evidence",
            {
                "q": "abc",
                "credential_id": str(CREDENTIAL_ID),
                "lim": 50,
            },
        )
    ]
    assert fake_db.table_calls == []
    assert body["match_count"] == 1
    assert body["matches"][0]["snippet"] == "<b>bitcoin</b> wallet dump"


def test_fts_error_falls_back_to_ilike_without_500(monkeypatch):
    rpc_error = Exception(
        "PGRST202 Could not find the function public.search_evidence(q, credential_id, lim)"
    )
    fake_db = _FakeDB(rpc_error=rpc_error, table_rows=LEGACY_ROWS)
    _use_db(monkeypatch, fake_db)

    body = _call(q="bitcoin", limit=50)

    assert fake_db.rpc_calls[0][0] == "search_evidence"
    assert fake_db.table_calls == ["exfiltrated_messages"]
    assert body["match_count"] == 1
    assert body["matches"] == LEGACY_ROWS


def test_fts_failure_warns_once(monkeypatch, caplog):
    fake_db = _FakeDB(rpc_error=Exception("PGRST202"), table_rows=LEGACY_ROWS)
    _use_db(monkeypatch, fake_db)

    with caplog.at_level(logging.WARNING, logger=search_mod.logger.name):
        _call(q="bitcoin")
        _call(q="bitcoin")

    warnings = [
        record
        for record in caplog.records
        if "falling back to ILIKE" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


def test_ilike_statement_timeout_degrades_to_200(monkeypatch):
    fake_db = _FakeDB(
        table_error=_statement_timeout_error(),
        rpc_error=Exception("Operator authorization required"),
    )
    _use_db(monkeypatch, fake_db)

    body = _call(q="telegram", limit=3)

    assert body["match_count"] == 0
    assert body["matches"] == []
    assert body["degraded"] == "query timed out"


def test_ilike_timeout_warns_once(monkeypatch, caplog):
    fake_db = _FakeDB(
        table_error=_statement_timeout_error(),
        rpc_error=Exception("Operator authorization required"),
    )
    _use_db(monkeypatch, fake_db)

    with caplog.at_level(logging.WARNING, logger=search_mod.logger.name):
        _call(q="telegram")
        _call(q="telegram")

    warnings = [
        record
        for record in caplog.records
        if "ILIKE fallback unavailable" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


def test_ilike_generic_failure_degrades_instead_of_500(monkeypatch):
    fake_db = _FakeDB(table_error=Exception("db down"), rpc_error=Exception("PGRST202"))
    _use_db(monkeypatch, fake_db)

    body = _call(q="bitcoin")

    assert body["match_count"] == 0
    assert body["matches"] == []
    assert body["degraded"] == "query unavailable"


def test_fts_success_path_never_touches_ilike(monkeypatch):
    fake_db = _FakeDB(rpc_rows=FTS_ROWS)
    _use_db(monkeypatch, fake_db)

    body = _call(q="telegram", limit=3)

    assert [name for name, _ in fake_db.rpc_calls] == ["search_evidence"]
    assert fake_db.table_calls == []
    assert body["match_count"] == 1
    assert body["matches"][0]["snippet"] == "<b>bitcoin</b> wallet dump"
    assert "degraded" not in body


def test_response_model_exposes_snippet_and_keeps_legacy_fields():
    assert "snippet" in SearchMatchOut.model_fields
    assert SearchMatchOut.model_fields["snippet"].default is None
    assert SearchOut.model_fields["degraded"].default is None

    payload = SearchOut(
        query="bitcoin",
        match_count=1,
        limit=50,
        matches=[
            {
                **LEGACY_ROWS[0],
                "snippet": "<b>bitcoin</b> wallet dump",
            }
        ],
    )
    dumped = payload.model_dump()
    assert dumped["matches"][0]["snippet"] == "<b>bitcoin</b> wallet dump"
    # Fields from the legacy row survive validation (extra="allow").
    assert dumped["matches"][0]["is_broadcasted"] is False
    assert dumped["matches"][0]["telegram_msg_id"] == 42


def test_route_serializes_snippet_without_dropping_existing_fields(client, monkeypatch):
    from app.api.routers import monitor

    fake_db = _FakeDB(rpc_rows=FTS_ROWS)
    monkeypatch.setattr(monitor, "db", fake_db)

    response = client.get("/monitor/search?q=bitcoin&limit=10", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "bitcoin"
    assert body["match_count"] == 1
    assert body["matches"][0]["snippet"] == "<b>bitcoin</b> wallet dump"
    assert body["matches"][0]["id"] == HIT_ID


def test_route_rejects_two_char_query_with_400(client):
    response = client.get("/monitor/search?q=ab", headers=AUTH)

    assert response.status_code == 400
    assert response.json()["detail"] == "query must be at least 3 chars"
