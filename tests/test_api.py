import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

# All monitor and scan routes require X-Monitor-Key header.
# Use the test key set in conftest.py.
AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}


def test_read_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@patch("app.api.routers.monitor.db")
def test_get_stats(mock_db):
    from app.api.routers import monitor

    monitor._STATS_CACHE = None
    # Trigger a DB error so we can test error handling without a real DB.
    mock_db.rpc.side_effect = Exception("DB Down")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(monitor.get_stats())
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Internal error"


@patch("app.api.routers.monitor.db")
def test_get_stats_uses_monitor_stats_rpc(mock_db):
    from app.api.routers import monitor

    monitor._STATS_CACHE = None
    mock_result = MagicMock()
    mock_result.data = [
        {
            "credentials_total": 10,
            "credentials_active": 3,
            "messages_exfiltrated": 200,
            "messages_broadcasted": 150,
        }
    ]
    mock_db.rpc.return_value.execute.return_value = mock_result

    stats = asyncio.run(monitor.get_stats())

    assert stats.credentials_total == 10
    assert stats.credentials_active == 3
    assert stats.messages_exfiltrated == 200
    assert stats.messages_broadcasted == 150
    mock_db.rpc.assert_called_once_with("get_monitor_stats")
    mock_db.table.assert_not_called()
    monitor._STATS_CACHE = None


@patch("app.api.routers.monitor.db")
def test_get_stats_serves_stale_cache_on_db_failure(mock_db, monkeypatch):
    from app.api.routers import monitor
    from app.schemas.models import StatsOut

    cached = StatsOut(
        credentials_total=1,
        credentials_active=1,
        messages_exfiltrated=2,
        messages_broadcasted=1,
    )
    monitor._STATS_CACHE = (0, cached)
    monkeypatch.setattr(monitor.time, "monotonic", lambda: 3600)
    mock_db.rpc.side_effect = Exception("DB Down")

    stats = asyncio.run(monitor.get_stats())

    assert stats.credentials_total == 1
    assert stats.credentials_active == 1
    assert stats.messages_exfiltrated == 2
    assert stats.messages_broadcasted == 1
    monitor._STATS_CACHE = None


def test_monitor_stats_fallback_uses_narrow_counts(monkeypatch):
    from app.api.routers import monitor

    class Result:
        def __init__(self, count):
            self.count = count
            self.data = []

    class Query:
        def __init__(self, table, calls):
            self.table = table
            self.calls = calls
            self.filter = None

        def select(self, columns, **kwargs):
            self.calls.append(("select", self.table, columns, kwargs))
            return self

        def limit(self, value):
            self.calls.append(("limit", self.table, value))
            return self

        def eq(self, column, value):
            self.filter = (column, value)
            self.calls.append(("eq", self.table, column, value))
            return self

        def execute(self):
            counts = {
                ("discovered_credentials", None): 10,
                ("discovered_credentials", ("status", "active")): 3,
                ("exfiltrated_messages", None): 200,
                ("exfiltrated_messages", ("is_broadcasted", True)): 150,
            }
            return Result(counts[(self.table, self.filter)])

    class Rpc:
        def execute(self):
            raise Exception("PGRST202 could not find function public.get_monitor_stats")

    class Db:
        def __init__(self):
            self.calls = []

        def rpc(self, name):
            assert name == "get_monitor_stats"
            return Rpc()

        def table(self, name):
            return Query(name, self.calls)

    fake_db = Db()
    monkeypatch.setattr(monitor, "db", fake_db)

    stats = monitor._get_monitor_stats()

    assert stats.credentials_total == 10
    assert stats.credentials_active == 3
    assert stats.messages_exfiltrated == 200
    assert stats.messages_broadcasted == 150
    selects = [call for call in fake_db.calls if call[0] == "select"]
    limits = [call for call in fake_db.calls if call[0] == "limit"]
    assert all(call[2] == "id" for call in selects)
    assert all(call[3] == {"count": "exact"} for call in selects)
    assert all(call[2] == 0 for call in limits)


@patch("app.workers.celery_app.app.send_task")
def test_trigger_scan(mock_send_task, client):
    mock_task = type("obj", (object,), {"id": "task-123"})
    mock_send_task.return_value = mock_task
    payload = {"source": "shodan", "query": "telegram"}
    response = client.post("/scan/trigger", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["task_id"] == "task-123"


def test_trigger_scan_invalid_source(client):
    payload = {"source": "invalid", "query": "telegram"}
    response = client.post("/scan/trigger", json=payload, headers=AUTH)
    assert response.status_code == 400


# --- Export endpoint tests ---

@patch("app.api.routers.monitor.db")
def test_export_json_default(mock_db, client):
    mock_result = MagicMock()
    mock_result.data = []
    mock_db.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = mock_result
    response = client.get("/monitor/export", headers=AUTH)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@patch("app.api.routers.monitor.db")
def test_export_csv_format(mock_db, client):
    mock_result = MagicMock()
    mock_result.data = []
    mock_db.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = mock_result
    response = client.get("/monitor/export?format=csv", headers=AUTH)
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert response.text.startswith("id,credential_id")


def test_export_auth_enforced(client):
    response = client.get("/monitor/export")
    assert response.status_code in (401, 403)


@patch("app.api.routers.monitor.db")
def test_export_since_filter(mock_db, client):
    mock_result = MagicMock()
    mock_result.data = []
    mock_db.table.return_value.select.return_value.order.return_value.limit.return_value.gte.return_value.execute.return_value = mock_result
    response = client.get("/monitor/export?since=2099-01-01T00:00:00Z", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == []


@patch("app.api.routers.monitor.db")
def test_export_limit_param(mock_db, client):
    mock_result = MagicMock()
    mock_result.data = [{
        "id": "abc", "credential_id": "cred-1", "telegram_msg_id": 1,
        "sender_name": "test", "content": "hi", "media_type": "text",
        "is_broadcasted": False, "created_at": "2025-01-01T00:00:00",
    }]
    mock_db.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = mock_result
    response = client.get("/monitor/export?limit=1", headers=AUTH)
    assert response.status_code == 200
    assert len(response.json()) <= 1
