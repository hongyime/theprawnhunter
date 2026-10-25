"""Unit tests for POST /monitor/semantic-search and its FTS fallback contract."""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers._monitor import search as search_mod

CREDENTIAL_ID = UUID("11111111-1111-1111-1111-111111111111")
MESSAGE_ID = "22222222-2222-2222-2222-222222222222"
VECTOR = [0.01] * 384

RPC_ROWS = [
    {
        "message_id": MESSAGE_ID,
        "credential_id": str(CREDENTIAL_ID),
        "distance": 0.25,
    }
]


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        self.calls.append((url, json))
        if self._error is not None:
            raise self._error
        return self._response


class _FakeRpcBuilder:
    def __init__(self, rows, error):
        self._rows = rows
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=self._rows)


class _FakeDB:
    def __init__(self, rows=None, error=None):
        self.rpc_calls = []
        self._rows = rows or []
        self._error = error

    def rpc(self, name, params=None):
        self.rpc_calls.append((name, params))
        return _FakeRpcBuilder(self._rows, self._error)


def _use_http(monkeypatch, response=None, error=None):
    client = _FakeAsyncClient(response=response, error=error)
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", lambda **kwargs: client)
    return client


def _use_db(monkeypatch, fake_db):
    monkeypatch.setattr(search_mod, "db", fake_db)
    return fake_db


def _call(**kwargs):
    return asyncio.run(search_mod.semantic_search(search_mod.SemanticSearchIn(**kwargs)))


@pytest.fixture(autouse=True)
def _embed_url(monkeypatch):
    monkeypatch.setenv("EMBED_API_URL", "http://embed.test")


def test_happy_path_embeds_then_calls_rpc(monkeypatch):
    client = _use_http(monkeypatch, _FakeResponse(200, {"vectors": [VECTOR]}))
    fake_db = _use_db(monkeypatch, _FakeDB(rows=RPC_ROWS))

    body = _call(q=" bitcoin wallet ", limit=25, credential_id=CREDENTIAL_ID)

    assert client.calls == [
        ("http://embed.test/embed", {"texts": ["bitcoin wallet"]}),
    ]
    assert fake_db.rpc_calls == [
        (
            "semantic_search",
            {
                "query_embedding": VECTOR,
                "credential_id": str(CREDENTIAL_ID),
                "lim": 25,
            },
        )
    ]
    assert body["fallback"] is False
    assert body["results"] == [
        {
            "message_id": MESSAGE_ID,
            "credential_id": str(CREDENTIAL_ID),
            "distance": 0.25,
        }
    ]


@pytest.mark.parametrize(("requested", "expected"), [(0, 1), (-10, 1), (25, 25), (500, 100)])
def test_limit_is_clamped_to_one_and_one_hundred(monkeypatch, requested, expected):
    _use_http(monkeypatch, _FakeResponse(200, {"vectors": [VECTOR]}))
    fake_db = _use_db(monkeypatch, _FakeDB(rows=[]))

    _call(q="bitcoin", limit=requested)

    assert fake_db.rpc_calls[0][1]["lim"] == expected


def test_embed_api_down_returns_fallback_instead_of_500(monkeypatch):
    _use_http(monkeypatch, error=RuntimeError("connection refused"))
    fake_db = _use_db(monkeypatch, _FakeDB(rows=RPC_ROWS))

    body = _call(q="bitcoin")

    assert body == {
        "results": [],
        "fallback": True,
        "reason": "embed_unreachable",
    }
    assert fake_db.rpc_calls == []


def test_unset_embed_url_uses_the_local_default(monkeypatch):
    monkeypatch.delenv("EMBED_API_URL")
    client = _use_http(monkeypatch, _FakeResponse(200, {"vectors": [VECTOR]}))
    _use_db(monkeypatch, _FakeDB(rows=[]))

    body = _call(q="bitcoin")

    assert client.calls[0][0] == "http://127.0.0.1:8020/embed"
    assert body["fallback"] is False


def test_blank_embed_url_degrades_without_a_network_call(monkeypatch):
    monkeypatch.setenv("EMBED_API_URL", "   ")
    client = _use_http(monkeypatch, _FakeResponse(200, {"vectors": [VECTOR]}))
    fake_db = _use_db(monkeypatch, _FakeDB(rows=[]))

    body = _call(q="bitcoin")

    assert client.calls == []
    assert fake_db.rpc_calls == []
    assert body == {
        "results": [],
        "fallback": True,
        "reason": "embed_api_url_unset",
    }


def test_embed_non_200_returns_fallback(monkeypatch):
    _use_http(monkeypatch, _FakeResponse(503, None))
    _use_db(monkeypatch, _FakeDB(rows=[]))

    body = _call(q="bitcoin")

    assert body["fallback"] is True
    assert body["results"] == []
    assert body["reason"] == "embed_http_503"


def test_embed_malformed_body_returns_fallback(monkeypatch):
    _use_http(monkeypatch, _FakeResponse(200, {"unexpected": True}))
    _use_db(monkeypatch, _FakeDB(rows=[]))

    body = _call(q="bitcoin")

    assert body["fallback"] is True
    assert body["reason"] == "embed_malformed_response"


def test_embed_wrong_dimensions_returns_fallback(monkeypatch):
    _use_http(monkeypatch, _FakeResponse(200, {"vectors": [[0.1, 0.2]]}))
    _use_db(monkeypatch, _FakeDB(rows=[]))

    body = _call(q="bitcoin")

    assert body["fallback"] is True
    assert body["reason"] == "embed_wrong_dimensions"


def test_rpc_failure_returns_fallback(monkeypatch):
    _use_http(monkeypatch, _FakeResponse(200, {"vectors": [VECTOR]}))
    _use_db(monkeypatch, _FakeDB(error=RuntimeError("PGRST202")))

    body = _call(q="bitcoin")

    assert body == {
        "results": [],
        "fallback": True,
        "reason": "rpc_unavailable",
    }


def test_empty_query_degrades_without_calling_services(monkeypatch):
    client = _use_http(monkeypatch, _FakeResponse(200, {"vectors": [VECTOR]}))
    fake_db = _use_db(monkeypatch, _FakeDB(rows=[]))

    body = _call(q="   ")

    assert client.calls == []
    assert fake_db.rpc_calls == []
    assert body == {
        "results": [],
        "fallback": True,
        "reason": "empty_query",
    }


def test_monitor_key_is_required(monkeypatch):
    from app.core import auth as auth_mod

    monkeypatch.setattr(auth_mod.settings, "MONITOR_API_KEY", "test-monitor-key")
    _use_http(monkeypatch, _FakeResponse(200, {"vectors": [VECTOR]}))
    _use_db(monkeypatch, _FakeDB(rows=[]))

    app = FastAPI()
    app.include_router(search_mod.router)
    client = TestClient(app)

    denied = client.post("/semantic-search", json={"q": "bitcoin"})
    assert denied.status_code == 403

    allowed = client.post(
        "/semantic-search",
        json={"q": "bitcoin"},
        headers={"X-Monitor-Key": "test-monitor-key"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["fallback"] is False
