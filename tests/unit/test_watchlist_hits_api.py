"""Unit tests for the watchlist hit deep-link monitor endpoint."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.api.routers._monitor import watchlist_hits as watchlist_hits_mod

AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}
HIT_ID = "22222222-2222-4222-8222-222222222222"
MESSAGE_ID = "44444444-4444-4444-8444-444444444444"
CREDENTIAL_ID = "11111111-1111-4111-8111-111111111111"
LINK = "https://t.me/c/1234567890/789/456"


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def select(self, columns):
        self.calls.append(("select", columns))
        return self

    def eq(self, column, value):
        self.calls.append(("eq", column, value))
        return self

    def limit(self, value):
        self.calls.append(("limit", value))
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._rows))


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.requested = []
        self.last_query = None

    def table(self, name):
        self.requested.append(name)
        self.last_query = _FakeQuery(self._rows)
        return self.last_query


def _patch(monkeypatch, *, rows, link=LINK):
    fake_db = _FakeDB(rows)
    resolver = AsyncMock(return_value=link)
    monkeypatch.setattr(watchlist_hits_mod, "db", fake_db)
    monkeypatch.setattr(watchlist_hits_mod, "_resolve_message_deeplink", resolver)
    return fake_db, resolver


def test_link_requires_the_monitor_key(client):
    response = client.get(f"/monitor/watchlist/hits/{HIT_ID}/link")

    assert response.status_code == 403


def test_link_returns_url_for_a_resolvable_hit(client, monkeypatch):
    fake_db, resolver = _patch(
        monkeypatch,
        rows=[{"message_id": MESSAGE_ID, "credential_id": CREDENTIAL_ID}],
    )

    response = client.get(
        f"/monitor/watchlist/hits/{HIT_ID}/link", headers=AUTH
    )

    assert response.status_code == 200
    assert response.json() == {"url": LINK}
    assert fake_db.requested == ["watchlist_hits"]
    assert fake_db.last_query.calls == [
        ("select", "message_id, credential_id"),
        ("eq", "id", HIT_ID),
        ("limit", 1),
    ]
    resolver.assert_awaited_once_with(
        message_id=MESSAGE_ID,
        credential_id=CREDENTIAL_ID,
    )


def test_link_is_null_when_the_hit_cannot_be_resolved(client, monkeypatch):
    _patch(
        monkeypatch,
        rows=[{"message_id": MESSAGE_ID, "credential_id": CREDENTIAL_ID}],
        link=None,
    )

    response = client.get(
        f"/monitor/watchlist/hits/{HIT_ID}/link", headers=AUTH
    )

    assert response.status_code == 200
    assert response.json() == {"url": None}


def test_link_returns_404_for_an_unknown_hit(client, monkeypatch):
    fake_db, resolver = _patch(monkeypatch, rows=[])

    response = client.get(
        f"/monitor/watchlist/hits/{HIT_ID}/link", headers=AUTH
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Watchlist hit not found"}
    assert fake_db.requested == ["watchlist_hits"]
    resolver.assert_not_awaited()
