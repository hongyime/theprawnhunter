"""Tests for HTTP error-response hygiene and CORS header restriction.

Covers:
- AUDIT-4: HTTPException(detail=...) must not leak raw exception strings.
- AUDIT-6: CORS allow_headers must be narrow (no wildcard).
"""
from unittest.mock import patch

import pytest

AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}


# ---------------------------------------------------------------------------
# AUDIT-4: 500-class errors should return a generic message
# ---------------------------------------------------------------------------
@patch("app.api.routers.monitor.db")
def test_stats_error_does_not_leak_exception(mock_db, client):
    """A DB failure should not echo 'Password: hunter2\\n...' back to the caller."""
    secret_marker = "DB Connection Error: postgres://user:PASSW0RD@host/db"
    mock_db.table.side_effect = Exception(secret_marker)

    response = client.get("/monitor/stats", headers=AUTH)
    assert response.status_code == 500
    body = response.text
    assert "PASSW0RD" not in body
    assert secret_marker not in body
    # Confirm we do return the generic form
    assert response.json()["detail"] == "Internal error"


@patch("app.api.routers.monitor.db")
def test_credentials_error_does_not_leak(mock_db, client):
    secret = "secret-string-leaked-via-supabase-error"
    mock_db.table.side_effect = Exception(secret)

    response = client.get("/monitor/credentials", headers=AUTH)
    assert response.status_code == 500
    assert secret not in response.text


@patch("app.api.routers.monitor.db")
def test_messages_error_does_not_leak(mock_db, client):
    secret = "boom-secret-token-abc123"
    mock_db.table.side_effect = Exception(secret)

    response = client.get("/monitor/messages", headers=AUTH)
    assert response.status_code == 500
    assert secret not in response.text


@patch("app.api.routers.monitor.db")
def test_search_error_does_not_leak(mock_db, client):
    secret = "sensitive-supabase-internal-detail"
    mock_db.table.side_effect = Exception(secret)

    response = client.get("/monitor/search?q=telegram", headers=AUTH)
    assert response.status_code == 500
    assert secret not in response.text


@patch("app.api.routers.monitor.db")
def test_webhooks_error_does_not_leak(mock_db, client):
    secret = "top-secret-supabase-dsn-token"
    mock_db.table.side_effect = Exception(secret)

    response = client.get("/monitor/webhooks", headers=AUTH)
    assert response.status_code == 500
    assert secret not in response.text


# ---------------------------------------------------------------------------
# AUDIT-6: CORS allow_headers must be an explicit allowlist
# ---------------------------------------------------------------------------
def test_cors_allow_headers_is_not_wildcard():
    """The CORS middleware must not allow every header."""
    from app.api.main import app

    for mw in app.user_middleware:
        # Different starlette/fastapi versions expose middleware config differently
        options = getattr(mw, "kwargs", None) or getattr(mw, "options", None) or {}
        allow_headers = options.get("allow_headers")
        if allow_headers is None:
            continue

        assert allow_headers != ["*"], (
            "CORSMiddleware allow_headers must be an explicit allowlist, not ['*']"
        )
        assert "*" not in allow_headers, (
            f"CORSMiddleware allow_headers must not contain '*'; got {allow_headers}"
        )
        # Positive: the header we actually use must be permitted
        assert any(h.lower() == "x-monitor-key" for h in allow_headers), (
            "X-Monitor-Key must be present in allow_headers"
        )
        return

    pytest.fail("CORSMiddleware not found in app.user_middleware")


def test_cors_preflight_advertises_narrow_headers(client):
    """A CORS preflight for the actual monitor route should echo only permitted headers."""
    response = client.options(
        "/monitor/stats",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Monitor-Key",
        },
    )
    # OPTIONS handled by CORSMiddleware — should be 200 for an allowed origin
    assert response.status_code == 200
    allowed = response.headers.get("access-control-allow-headers", "").lower()
    assert "x-monitor-key" in allowed
    assert "*" not in allowed
