"""Regression tests for the read-only schema drift command."""

from __future__ import annotations

import json
import sys

from scripts import schema_drift_check


class _Response:
    def __init__(self, status: int):
        self.status = status
        self.read_called = False

    def read(self) -> bytes:
        self.read_called = True
        return b"[]"


class _Connection:
    responses: list[_Response] = []
    paths: list[str] = []

    def __init__(self, *_args, **_kwargs):
        self._next = 0

    def request(self, _method: str, path: str, **_kwargs) -> None:
        self.paths.append(path)

    def getresponse(self) -> _Response:
        response = self.responses[self._next]
        self._next += 1
        return response

    def close(self) -> None:
        return None


def test_rest_fallback_fails_closed_on_probe_error(monkeypatch, capsys):
    responses = [_Response(200), _Response(500), _Response(200)]
    _Connection.responses = responses
    _Connection.paths = []
    monkeypatch.setattr("http.client.HTTPSConnection", _Connection)

    result = schema_drift_check._check_schema_via_rest(
        "https://example.supabase.co",
        "test-service-role-key",
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["status"] == "failed"
    assert all(response.read_called for response in responses)
    assert all("select=*" not in path for path in _Connection.paths)
    assert "id" in _Connection.paths[0]
    assert "bot_token" in _Connection.paths[0]


def test_direct_database_path_reports_missing_psql(monkeypatch, capsys):
    monkeypatch.setattr(schema_drift_check, "_load_dotenv_if_needed", lambda: None)
    monkeypatch.setattr(schema_drift_check.shutil, "which", lambda _name: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/test")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["schema_drift_check.py"])

    result = schema_drift_check.main()

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload == {
        "status": "blocked",
        "reason": "psql executable not found in PATH",
    }
