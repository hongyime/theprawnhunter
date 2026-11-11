from __future__ import annotations

from unittest.mock import patch

AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table_name: str, rows: list[dict], calls: list[dict]):
        self.table_name = table_name
        self.rows = rows
        self.calls = calls
        self.record = {"table": table_name}
        self.calls.append(self.record)

    def select(self, columns: str, *args, **kwargs):
        self.record["select"] = columns
        return self

    def in_(self, column: str, values: list[str]):
        self.record["in"] = (column, values)
        return self

    def order(self, column: str, **kwargs):
        self.record["order"] = (column, kwargs)
        return self

    def limit(self, value: int):
        self.record["limit"] = value
        self.rows = self.rows[:value]
        return self

    def execute(self):
        return _Result(self.rows)


class _Db:
    def __init__(self):
        self.calls: list[dict] = []
        self.tables = {
            "telemetry_indicators": [
                {
                    "indicator_type": "network_domain",
                    "indicator_value": "Api.Example.COM.",
                    "first_seen_at": "2026-08-07T00:00:00Z",
                    "raw_context": {"content": "must-not-leak"},
                },
                {
                    "indicator_type": "canonical_url",
                    "indicator_value": "https://Portal.Example.com/app?token=drop",
                    "first_seen_at": "2026-08-07T00:01:00Z",
                },
                {
                    "indicator_type": "network_domain",
                    "indicator_value": "api.example.com",
                    "first_seen_at": "2026-08-07T00:02:00Z",
                },
                {
                    "indicator_type": "wallet_address",
                    "indicator_value": "0x1111111111111111111111111111111111111111",
                    "first_seen_at": "2026-08-07T00:03:00Z",
                },
            ],
            "discovered_credentials": [
                {
                    "source": "validation",
                    "status": "active",
                    "meta": {
                        "webhook_url": "https://Hook.Example.net/telegram/123?secret=drop",
                        "webhook_probe": {"tls_san": ["*.example.net"]},
                        "token": "must-not-leak",
                    },
                    "created_at": "2026-08-06T00:00:00Z",
                    "updated_at": "2026-08-07T00:00:00Z",
                    "bot_token": "must-not-leak",
                    "token_hash": "must-not-leak",
                }
            ],
        }

    def table(self, name: str):
        return _Query(name, list(self.tables[name]), self.calls)


def test_target_feed_export_requires_monitor_key(client):
    response = client.get("/monitor/targets/export")

    assert response.status_code == 403


def test_target_feed_export_sanitizes_and_dedupes(client):
    fake_db = _Db()

    with patch("app.api.routers._monitor.common.db", fake_db):
        response = client.get("/monitor/targets/export?limit=10", headers=AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "target-feed.v1"
    assert isinstance(payload["generated_at"], str)
    assert payload["items"] == [
        {
            "target_type": "domain",
            "target_value": "api.example.com",
            "source_kind": "telemetry_indicator",
            "confidence": 0.85,
            "first_seen_at": "2026-08-07T00:00:00Z",
            "provenance": "telemetry_indicators.network_domain",
        },
        {
            "target_type": "url",
            "target_value": "https://portal.example.com/app",
            "source_kind": "telemetry_indicator",
            "confidence": 0.9,
            "first_seen_at": "2026-08-07T00:01:00Z",
            "provenance": "telemetry_indicators.canonical_url",
        },
        {
            "target_type": "url",
            "target_value": "https://hook.example.net/telegram/123",
            "source_kind": "credential_metadata",
            "confidence": 0.8,
            "first_seen_at": "2026-08-07T00:00:00Z",
            "provenance": "discovered_credentials.meta.webhook_url",
        },
    ]

    serialized = response.text
    assert "must-not-leak" not in serialized
    assert "token_hash" not in serialized
    assert "bot_token" not in serialized
    assert "raw_context" not in serialized
    assert "webhook_probe" not in serialized

    selects = {call["table"]: call["select"] for call in fake_db.calls}
    assert selects["telemetry_indicators"] == "indicator_type, indicator_value, first_seen_at"
    assert selects["discovered_credentials"] == "source, status, meta, created_at, updated_at"


def test_target_feed_export_applies_limit_after_dedupe(client):
    fake_db = _Db()

    with patch("app.api.routers._monitor.common.db", fake_db):
        response = client.get("/monitor/targets/export?limit=1", headers=AUTH)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
