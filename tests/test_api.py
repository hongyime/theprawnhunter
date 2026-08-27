import pytest
from unittest.mock import MagicMock, patch

# All monitor and scan routes require X-Monitor-Key header.
# Use the test key set in conftest.py.
AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}


def test_read_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@patch("app.api.routers.monitor.db")
def test_get_stats(mock_db, client):
    # Trigger a DB error so we can test error handling without a real DB.
    mock_db.table.side_effect = Exception("DB Down")
    response = client.get("/monitor/stats", headers=AUTH)
    # Monitor router catches exceptions and returns 500
    assert response.status_code == 500


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
