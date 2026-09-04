"""Tests for the centralized monitor-key auth dependency.

Addresses AUDIT-1 (media auth), AUDIT-2 (constant-time compare), and
AUDIT-3 (single-source auth). These tests must pass alongside the
existing test_api.py suite.
"""
import inspect
from unittest.mock import patch

import pytest

AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}


# ---------------------------------------------------------------------------
# AUDIT-1: /media/{id} must require the same key as /monitor/*
# ---------------------------------------------------------------------------
def test_media_endpoint_requires_auth(client):
    # No header at all
    response = client.get("/media/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 403

    # Wrong key
    response = client.get(
        "/media/00000000-0000-0000-0000-000000000000",
        headers={"X-Monitor-Key": "obviously-wrong"},
    )
    assert response.status_code == 403


def test_media_endpoint_accepts_valid_key(client):
    """Valid key should reach DB layer — mocked DB returns 404, which is expected."""
    with patch("app.api.routers.media.db") as mock_db:
        # single().execute() raises for a bogus UUID → endpoint returns 404
        mock_db.table.return_value.select.return_value.eq.return_value.single.return_value.execute.side_effect = Exception("not found")
        response = client.get(
            "/media/00000000-0000-0000-0000-000000000000",
            headers=AUTH,
        )
    # Valid key means we get PAST the auth gate. Any status other than 403 proves auth passed.
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# AUDIT-2: constant-time compare — asserted indirectly.
#   1. Ensure hmac.compare_digest is used (import-level check).
#   2. Ensure the endpoint returns 403 for a key that shares a long prefix
#      with the real key but differs at the end.
# ---------------------------------------------------------------------------
def test_auth_uses_hmac_compare_digest():
    """The auth module must import hmac and reference compare_digest."""
    from app.core import auth as auth_module

    src = inspect.getsource(auth_module)
    assert "hmac.compare_digest" in src, (
        "Monitor-key auth must use hmac.compare_digest to avoid timing side-channels"
    )


def test_close_prefix_key_rejected(client):
    """A near-match key must be rejected without leaking timing info."""
    # Real key is 'test-monitor-key-for-pytest' — try one that shares the prefix
    near_miss = "test-monitor-key-for-pytesX"
    response = client.get("/monitor/stats", headers={"X-Monitor-Key": near_miss})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# AUDIT-3: All protected endpoints reject requests without a key.
# We iterate to catch a regression where a new endpoint is added without
# the router-level dependency.
# ---------------------------------------------------------------------------
PROTECTED_ENDPOINTS = [
    ("GET",  "/monitor/stats"),
    ("GET",  "/monitor/credentials"),
    ("GET",  "/monitor/messages"),
    ("GET",  "/monitor/broadcasts/pending"),
    ("POST", "/monitor/broadcasts/00000000-0000-0000-0000-000000000000/retry"),
    ("POST", "/monitor/topics/revoked/close"),
    ("GET",  "/monitor/webhooks"),
    ("GET",  "/monitor/targets/export"),
    ("GET",  "/monitor/search?q=abc"),
    ("GET",  "/monitor/operators"),
    ("GET",  "/monitor/findings"),
    ("GET",  "/monitor/findings/00000000-0000-0000-0000-000000000000"),
    ("POST", "/monitor/findings/00000000-0000-0000-0000-000000000000/feedback"),
    ("POST", "/monitor/engagement/lifecycle"),
    ("GET",  "/health/detailed"),
    ("GET",  "/health/metrics"),
    ("GET",  "/health/queues"),
    ("GET",  "/health/operational"),
    ("GET",  "/health/quotas"),
    ("GET",  "/health/bot-pool"),
    ("GET",  "/health/circuit-breakers"),
    ("POST", "/health/circuit-breakers/shodan/reset"),
    ("GET",  "/media/00000000-0000-0000-0000-000000000000"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
def test_protected_endpoint_requires_key(client, method, path):
    fn = getattr(client, method.lower())
    resp = fn(path)
    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code} without X-Monitor-Key "
        f"(expected 403). Router is likely missing require_monitor_key."
    )


# ---------------------------------------------------------------------------
# The /health/ liveness endpoint must remain public — used by docker healthchecks
# ---------------------------------------------------------------------------
def test_health_liveness_stays_public(client):
    resp = client.get("/health/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
