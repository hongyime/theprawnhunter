"""Outpost runner: CLI, payload shaping, and retry semantics (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "outpost_runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("outpost_runner_under_test", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()

LEASE_URL = "http://api.test:8011/monitor/outposts/lease"
RESULT_URL = "http://api.test:8011/monitor/outposts/result"
TOKEN = "12345678:super-secret-token"
MONITOR_KEY = "monitor-secret-key"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _lease_payload(**overrides):
    payload = {
        "lease_id": "lease-1",
        "expires_at": "2026-10-25T00:00:00Z",
        "candidates": [
            {
                "queue_row_id": "row-1",
                "token": TOKEN,
                "source": "github",
                "meta": {},
            }
        ],
    }
    payload.update(overrides)
    return payload


def _get_me_ok():
    return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "bot"}}


# ── CLI parsing ────────────────────────────────────────────────────────────


def test_cli_defaults(monkeypatch):
    monkeypatch.delenv("OUTPOST_API_BASE", raising=False)

    args = RUNNER._parser().parse_args([])

    assert args.api_base == "http://127.0.0.1:8011"
    assert args.monitor_key_env == "MONITOR_API_KEY"
    assert args.telegram_api_base == "https://api.telegram.org"
    assert args.once is False
    assert args.dry_run is False
    assert args.poll_seconds == RUNNER.DEFAULT_POLL_SECONDS
    assert args.backoff_max_seconds == RUNNER.DEFAULT_BACKOFF_MAX_SECONDS


def test_api_base_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OUTPOST_API_BASE", "http://10.0.0.5:9000")

    args = RUNNER._parser().parse_args([])

    assert args.api_base == "http://10.0.0.5:9000"


def test_monitor_key_is_only_accepted_as_an_env_var_name():
    parser = RUNNER._parser()

    args = parser.parse_args(["--monitor-key-env", "OUTPOST_KEY"])
    assert args.monitor_key_env == "OUTPOST_KEY"

    # The docs spelling accepts only the literal "env".
    args = parser.parse_args(["--monitor-key", "env"])
    assert args.monitor_key_env == "MONITOR_API_KEY"

    # A plaintext key on argv is never accepted.
    with pytest.raises(SystemExit):
        parser.parse_args(["--monitor-key", MONITOR_KEY])


# ── Payload shaping (pure) ─────────────────────────────────────────────────


def test_build_lease_request_uses_monitor_header():
    request = RUNNER.build_lease_request("http://api.test:8011/", MONITOR_KEY)

    assert request["method"] == "POST"
    assert request["url"] == LEASE_URL
    assert request["headers"]["X-Monitor-Key"] == MONITOR_KEY


def test_build_result_payload_shape():
    payload = RUNNER.build_result_payload(
        lease_id="lease-1",
        queue_row_id="row-1",
        ok=True,
        http_status=200,
        reason="ok",
    )

    assert payload == {
        "lease_id": "lease-1",
        "queue_row_id": "row-1",
        "ok": True,
        "http_status": 200,
        "reason": "ok",
    }


def test_classify_success():
    verdict = RUNNER.classify_telegram_response(200, _get_me_ok())

    assert verdict.ok is True
    assert verdict.http_status == 200
    assert verdict.reason == "ok"


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, {"ok": False, "error_code": 401}, "unauthorized"),
        (404, {"ok": False, "error_code": 404}, "unauthorized"),
        (403, {"ok": False, "error_code": 403}, "forbidden"),
        (500, {"ok": False, "error_code": 500}, "telegram_error"),
        (400, {"ok": False, "error_code": 400}, "telegram_rejected"),
        (200, None, "invalid_response"),
    ],
)
def test_classify_failures(status, body, reason):
    verdict = RUNNER.classify_telegram_response(status, body)

    assert verdict.ok is False
    assert verdict.reason == reason


def test_classify_flood_wait_keeps_retry_after():
    verdict = RUNNER.classify_telegram_response(
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 7",
            "parameters": {"retry_after": 7},
        },
    )

    assert verdict.ok is False
    assert verdict.reason == "flood_wait"
    assert verdict.retry_after == 7.0


def test_mask_token_never_reveals_suffix():
    assert RUNNER.mask_token(TOKEN) == "12345678:…"
    assert "super-secret" not in RUNNER.mask_token(TOKEN)


# ── Async cycle behaviour ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cycle_sends_lease_header_and_shaped_result(caplog):
    calls: list[httpx.Request] = []
    result_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url) == LEASE_URL:
            return httpx.Response(200, json=_lease_payload())
        if request.url.host == "api.telegram.org":
            assert str(request.url).endswith(f"/bot{TOKEN}/getMe")
            return httpx.Response(200, json=_get_me_ok())
        if str(request.url) == RESULT_URL:
            result_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.url}")

    async def no_sleep(seconds: float) -> None:
        raise AssertionError("successful cycle must not sleep")

    async with _client(handler) as client:
        state = await RUNNER.run_cycle(
            client,
            api_base="http://api.test:8011",
            monitor_key=MONITOR_KEY,
            telegram_api_base="https://api.telegram.org",
            backoff_max_seconds=30.0,
            sleep=no_sleep,
        )

    assert state == "ok"
    assert calls[0].method == "POST"
    assert calls[0].headers["X-Monitor-Key"] == MONITOR_KEY
    assert result_bodies == [
        {
            "lease_id": "lease-1",
            "queue_row_id": "row-1",
            "ok": True,
            "http_status": 200,
            "reason": "ok",
        }
    ]
    assert MONITOR_KEY not in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_telegram_429_backs_off_and_reports_flood_wait():
    result_bodies: list[dict] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LEASE_URL:
            return httpx.Response(200, json=_lease_payload())
        if request.url.host == "api.telegram.org":
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 7",
                    "parameters": {"retry_after": 7},
                },
            )
        if str(request.url) == RESULT_URL:
            result_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.url}")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with _client(handler) as client:
        state = await RUNNER.run_cycle(
            client,
            api_base="http://api.test:8011",
            monitor_key=MONITOR_KEY,
            telegram_api_base="https://api.telegram.org",
            backoff_max_seconds=30.0,
            sleep=fake_sleep,
        )

    assert state == "ok"
    assert sleeps == [7.0]
    assert result_bodies == [
        {
            "lease_id": "lease-1",
            "queue_row_id": "row-1",
            "ok": False,
            "http_status": 429,
            "reason": "flood_wait",
        }
    ]


@pytest.mark.asyncio
async def test_flood_wait_sleep_is_bounded_by_backoff_max():
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LEASE_URL:
            return httpx.Response(200, json=_lease_payload())
        if request.url.host == "api.telegram.org":
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 9999},
                },
            )
        return httpx.Response(200, json={"status": "ok"})

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with _client(handler) as client:
        await RUNNER.run_cycle(
            client,
            api_base="http://api.test:8011",
            monitor_key=MONITOR_KEY,
            telegram_api_base="https://api.telegram.org",
            backoff_max_seconds=12.5,
            sleep=fake_sleep,
        )

    assert sleeps == [12.5]


@pytest.mark.asyncio
async def test_telegram_network_error_retries_then_reports_without_crashing():
    result_bodies: list[dict] = []
    sleeps: list[float] = []
    telegram_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal telegram_attempts
        if str(request.url) == LEASE_URL:
            return httpx.Response(200, json=_lease_payload())
        if request.url.host == "api.telegram.org":
            telegram_attempts += 1
            raise httpx.ConnectError("boom", request=request)
        if str(request.url) == RESULT_URL:
            result_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.url}")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with _client(handler) as client:
        state = await RUNNER.run_cycle(
            client,
            api_base="http://api.test:8011",
            monitor_key=MONITOR_KEY,
            telegram_api_base="https://api.telegram.org",
            backoff_max_seconds=30.0,
            sleep=fake_sleep,
        )

    assert state == "ok"
    assert telegram_attempts == RUNNER.MAX_TOKEN_ATTEMPTS
    assert sleeps == [1.0, 2.0]
    assert result_bodies[0]["ok"] is False
    assert result_bodies[0]["http_status"] is None
    assert result_bodies[0]["reason"] == "network_error"


# ── main() edge cases ──────────────────────────────────────────────────────


def test_403_from_lease_exits_cleanly(monkeypatch, caplog):
    monkeypatch.setenv("MONITOR_API_KEY", MONITOR_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "outposts disabled"})

    monkeypatch.setattr(RUNNER, "_make_client", lambda: _client(handler))

    assert RUNNER.main(["--once"]) == 0
    assert "Outposts are disabled" in caplog.text


def test_api_500_is_retried_then_once_exits_cleanly(monkeypatch, caplog):
    monkeypatch.setenv("MONITOR_API_KEY", MONITOR_KEY)
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, json={"detail": "boom"})

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(RUNNER, "_make_client", lambda: _client(handler))
    monkeypatch.setattr(RUNNER, "_sleep", fake_sleep)

    assert RUNNER.main(["--once"]) == 0
    assert attempts == RUNNER.MAX_API_ATTEMPTS
    assert sleeps == [1.0, 2.0]
    assert "HTTP 500" in caplog.text


def test_network_failure_on_lease_exits_cleanly_under_once(monkeypatch):
    monkeypatch.setenv("MONITOR_API_KEY", MONITOR_KEY)
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(RUNNER, "_make_client", lambda: _client(handler))
    monkeypatch.setattr(RUNNER, "_sleep", fake_sleep)

    assert RUNNER.main(["--once"]) == 0
    assert sleeps == [1.0, 2.0]


def test_dry_run_makes_no_network_calls(monkeypatch, capsys):
    monkeypatch.setenv("MONITOR_API_KEY", TOKEN)

    def explode():
        raise AssertionError("dry-run must not create an HTTP client")

    monkeypatch.setattr(RUNNER, "_make_client", explode)

    assert RUNNER.main(["--dry-run", "--api-base", "http://api.test:8011"]) == 0

    output = capsys.readouterr().out
    assert "no lease, Telegram, or result calls" in output
    assert LEASE_URL in output
    assert RESULT_URL in output
    assert "monitor_key_env=MONITOR_API_KEY" in output
    assert "12345678:…" in output
    assert TOKEN not in output


def test_missing_monitor_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("MONITOR_API_KEY", raising=False)

    assert RUNNER.main(["--once"]) == 2
    assert "MONITOR_API_KEY is not set" in capsys.readouterr().err
