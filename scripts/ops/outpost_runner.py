"""Standalone outpost validation runner.

An outpost leases candidate tokens from the monitor API, checks each one with
Telegram's ``getMe`` endpoint, and reports the verdict back. It needs no
database, Redis, or Celery access, so it can run on a separate VM.

Examples:
    # Show the configuration without making any network call.
    python scripts/ops/outpost_runner.py --api-base http://host:8011 --dry-run

    # One lease -> getMe -> result cycle for smoke testing.
    python scripts/ops/outpost_runner.py --once

    # Forever loop, polling every 30 seconds between leases.
    MONITOR_API_KEY=... python scripts/ops/outpost_runner.py

The monitor key is read from the environment (default ``MONITOR_API_KEY``);
it is never accepted as a command-line value so it cannot leak into shell
history or ``ps`` output.

Dry-run semantics: ``--dry-run`` performs *no* lease call, no Telegram call,
and no result call. It must not consume a lease, so it only prints the
effective configuration (endpoints, timings, and whether a key is present).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

LOGGER = logging.getLogger("outpost_runner")

DEFAULT_API_BASE = "http://127.0.0.1:8011"
DEFAULT_MONITOR_KEY_ENV = "MONITOR_API_KEY"
DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_BACKOFF_MAX_SECONDS = 300.0
DEFAULT_FLOOD_WAIT_SECONDS = 5.0
BASE_BACKOFF_SECONDS = 1.0
TELEGRAM_TIMEOUT_SECONDS = 10.0
API_TIMEOUT_SECONDS = 30.0
MAX_API_ATTEMPTS = 3
MAX_TOKEN_ATTEMPTS = 3

LEASE_PATH = "/monitor/outposts/lease"
RESULT_PATH = "/monitor/outposts/result"


class ConfigError(RuntimeError):
    """Raised when a real run is missing required configuration."""


@dataclasses.dataclass(frozen=True)
class TelegramVerdict:
    """Normalized result of one Telegram ``getMe`` check."""

    ok: bool
    http_status: int | None
    reason: str
    retry_after: float | None = None


# ── Pure helpers (unit-testable without HTTP) ───────────────────────────────


def mask_token(token: str) -> str:
    """Return a log-safe token label. Never returns the secret suffix."""
    if not token:
        return "<missing>"
    prefix, sep, _ = token.partition(":")
    if sep and len(prefix) >= 4:
        return f"{prefix}:…"
    return f"{token[:4]}…"


def build_lease_request(api_base: str, monitor_key: str) -> dict[str, Any]:
    """Build the lease POST description (URL + authenticated headers)."""
    return {
        "method": "POST",
        "url": f"{api_base.rstrip('/')}{LEASE_PATH}",
        "headers": {
            "X-Monitor-Key": monitor_key,
            "Content-Type": "application/json",
        },
    }


def build_result_request(api_base: str, monitor_key: str) -> dict[str, Any]:
    """Build the result POST description (URL + authenticated headers)."""
    return {
        "method": "POST",
        "url": f"{api_base.rstrip('/')}{RESULT_PATH}",
        "headers": {
            "X-Monitor-Key": monitor_key,
            "Content-Type": "application/json",
        },
    }


def build_result_payload(
    *,
    lease_id: str | None,
    queue_row_id: Any,
    ok: bool,
    http_status: int | None,
    reason: str,
) -> dict[str, Any]:
    """Shape the body posted to ``/monitor/outposts/result``."""
    return {
        "lease_id": lease_id,
        "queue_row_id": queue_row_id,
        "ok": bool(ok),
        "http_status": http_status,
        "reason": reason,
    }


def classify_telegram_response(
    status_code: int,
    payload: Any,
) -> TelegramVerdict:
    """Classify a Telegram ``getMe`` response without needing HTTP.

    Args:
        status_code: HTTP status returned by Telegram.
        payload: Decoded JSON body (or ``None`` when it was not JSON).
    """
    body = payload if isinstance(payload, dict) else {}
    description = body.get("description")
    retry_after = _coerce_number(body.get("parameters", {}).get("retry_after"))
    if retry_after is None:
        retry_after = _coerce_number(body.get("retry_after"))

    error_code = _coerce_int(body.get("error_code"))
    effective_status = status_code if status_code else (error_code or 0)

    if effective_status == 429 or error_code == 429:
        return TelegramVerdict(
            ok=False,
            http_status=429,
            reason="flood_wait",
            retry_after=retry_after,
        )
    if 200 <= effective_status < 300 and body.get("ok") is True and "result" in body:
        return TelegramVerdict(ok=True, http_status=status_code or 200, reason="ok")
    if effective_status in (401, 404):
        return TelegramVerdict(
            ok=False,
            http_status=effective_status,
            reason="unauthorized",
        )
    if effective_status == 403:
        return TelegramVerdict(ok=False, http_status=403, reason="forbidden")
    if 500 <= effective_status < 600:
        return TelegramVerdict(
            ok=False,
            http_status=effective_status,
            reason="telegram_error",
        )
    if not body:
        return TelegramVerdict(
            ok=False,
            http_status=status_code or None,
            reason="invalid_response",
        )
    reason = "telegram_rejected"
    if isinstance(description, str) and description.strip():
        reason = "telegram_rejected"
    return TelegramVerdict(
        ok=False,
        http_status=effective_status or status_code or None,
        reason=reason,
    )


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decode_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _backoff_delay(attempt: int, cap: float) -> float:
    """Exponential backoff for a 1-based attempt number, bounded by ``cap``."""
    return min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), max(cap, 0.0))


# ── Async network seam ─────────────────────────────────────────────────────


async def _sleep(seconds: float) -> None:
    """Indirection so tests can record backoff without waiting."""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _make_client() -> httpx.AsyncClient:
    """Create the HTTP client used by a run (monkeypatched in tests)."""
    return httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS)


async def _lease_once(
    client: httpx.AsyncClient,
    *,
    request: dict[str, Any],
    max_attempts: int,
    backoff_max_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
    log: logging.Logger,
) -> tuple[httpx.Response | None, str]:
    """POST the lease request, retrying 5xx/network failures with backoff.

    Returns ``(response, state)`` where state is one of ``ok``, ``forbidden``,
    ``api_error``, or ``api_unavailable``.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(
                request["url"],
                headers=request["headers"],
                json={},
            )
        except httpx.RequestError as exc:
            log.warning(
                "Lease request failed (%s) attempt %d/%d",
                type(exc).__name__,
                attempt,
                max_attempts,
            )
            if attempt >= max_attempts:
                return None, "api_unavailable"
            await sleep(_backoff_delay(attempt, backoff_max_seconds))
            continue

        if response.status_code == 403:
            return response, "forbidden"
        if response.status_code >= 500:
            log.warning(
                "Lease API returned HTTP %d attempt %d/%d",
                response.status_code,
                attempt,
                max_attempts,
            )
            if attempt >= max_attempts:
                return response, "api_unavailable"
            await sleep(_backoff_delay(attempt, backoff_max_seconds))
            continue
        if 200 <= response.status_code < 300:
            return response, "ok"
        log.error("Lease API rejected the request with HTTP %d", response.status_code)
        return response, "api_error"
    return None, "api_unavailable"


async def _check_token(
    client: httpx.AsyncClient,
    token: str,
    *,
    telegram_api_base: str,
    backoff_max_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
    log: logging.Logger,
) -> TelegramVerdict:
    """Call Telegram ``getMe`` for one token, with bounded per-token backoff."""
    url = f"{telegram_api_base.rstrip('/')}/bot{token}/getMe"
    for attempt in range(1, MAX_TOKEN_ATTEMPTS + 1):
        try:
            response = await client.get(url, timeout=TELEGRAM_TIMEOUT_SECONDS)
        except httpx.RequestError as exc:
            log.warning(
                "getMe network error for token %s (%s) attempt %d/%d",
                mask_token(token),
                type(exc).__name__,
                attempt,
                MAX_TOKEN_ATTEMPTS,
            )
            if attempt >= MAX_TOKEN_ATTEMPTS:
                return TelegramVerdict(ok=False, http_status=None, reason="network_error")
            await sleep(_backoff_delay(attempt, backoff_max_seconds))
            continue

        verdict = classify_telegram_response(response.status_code, _decode_json(response))
        if verdict.reason == "flood_wait":
            delay = verdict.retry_after
            if delay is None:
                delay = DEFAULT_FLOOD_WAIT_SECONDS
            bounded = min(max(delay, 0.0), max(backoff_max_seconds, 0.0))
            log.warning(
                "getMe rate-limited for token %s; sleeping %.1fs",
                mask_token(token),
                bounded,
            )
            await sleep(bounded)
        return verdict
    return TelegramVerdict(ok=False, http_status=None, reason="network_error")


async def _post_result(
    client: httpx.AsyncClient,
    *,
    request: dict[str, Any],
    payload: dict[str, Any],
    max_attempts: int,
    backoff_max_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
    log: logging.Logger,
) -> bool:
    """POST one result, retrying transient failures. Never raises."""
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(
                request["url"],
                headers=request["headers"],
                json=payload,
            )
        except httpx.RequestError as exc:
            log.warning(
                "Result POST failed (%s) queue_row_id=%s attempt %d/%d",
                type(exc).__name__,
                payload.get("queue_row_id"),
                attempt,
                max_attempts,
            )
            if attempt >= max_attempts:
                return False
            await sleep(_backoff_delay(attempt, backoff_max_seconds))
            continue

        if 200 <= response.status_code < 300:
            return True
        if response.status_code >= 500:
            log.warning(
                "Result API returned HTTP %d for queue_row_id=%s attempt %d/%d",
                response.status_code,
                payload.get("queue_row_id"),
                attempt,
                max_attempts,
            )
            if attempt >= max_attempts:
                return False
            await sleep(_backoff_delay(attempt, backoff_max_seconds))
            continue
        log.error(
            "Result API rejected queue_row_id=%s with HTTP %d",
            payload.get("queue_row_id"),
            response.status_code,
        )
        return False
    return False


async def run_cycle(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    monitor_key: str,
    telegram_api_base: str,
    backoff_max_seconds: float,
    max_api_attempts: int = MAX_API_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    log: logging.Logger | None = None,
) -> str:
    """Run one lease -> getMe -> result cycle.

    Returns a state string: ``ok``, ``no_candidates``, ``forbidden``,
    ``api_error``, or ``api_unavailable``.
    """
    sleep = sleep or _sleep
    log = log or LOGGER
    lease_request = build_lease_request(api_base, monitor_key)
    response, state = await _lease_once(
        client,
        request=lease_request,
        max_attempts=max_api_attempts,
        backoff_max_seconds=backoff_max_seconds,
        sleep=sleep,
        log=log,
    )
    if state == "forbidden":
        log.error(
            "Outposts are disabled (HTTP 403). Set OUTPOST_ENABLED=True on the "
            "monitor API before running the outpost runner."
        )
        return "forbidden"
    if state != "ok" or response is None:
        return state

    data = _decode_json(response)
    if not isinstance(data, dict):
        log.error("Lease API returned a non-JSON payload")
        return "api_error"

    lease_id = data.get("lease_id")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        log.info("Lease returned no candidates")
        return "no_candidates"

    result_request = build_result_request(api_base, monitor_key)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        token = candidate.get("token")
        queue_row_id = candidate.get("queue_row_id")
        if not isinstance(token, str) or not token:
            verdict = TelegramVerdict(ok=False, http_status=None, reason="missing_token")
        else:
            verdict = await _check_token(
                client,
                token,
                telegram_api_base=telegram_api_base,
                backoff_max_seconds=backoff_max_seconds,
                sleep=sleep,
                log=log,
            )

        payload = build_result_payload(
            lease_id=lease_id,
            queue_row_id=queue_row_id,
            ok=verdict.ok,
            http_status=verdict.http_status,
            reason=verdict.reason,
        )
        log.info(
            "Result queue_row_id=%s source=%s ok=%s status=%s reason=%s token=%s",
            queue_row_id,
            candidate.get("source"),
            verdict.ok,
            verdict.http_status,
            verdict.reason,
            mask_token(token) if isinstance(token, str) else "<missing>",
        )
        await _post_result(
            client,
            request=result_request,
            payload=payload,
            max_attempts=max_api_attempts,
            backoff_max_seconds=backoff_max_seconds,
            sleep=sleep,
            log=log,
        )
    return "ok"


async def _run(
    args: argparse.Namespace,
    monitor_key: str,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> int:
    """Run the poll loop. ``--once`` performs exactly one cycle."""
    client_factory = client_factory or _make_client
    sleep = sleep or _sleep
    async with client_factory() as client:
        while True:
            state = await run_cycle(
                client,
                api_base=args.api_base,
                monitor_key=monitor_key,
                telegram_api_base=args.telegram_api_base,
                backoff_max_seconds=args.backoff_max_seconds,
                sleep=sleep,
            )
            if state == "forbidden":
                return 0
            if args.once:
                return 0
            await sleep(args.poll_seconds)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _monitor_key_mode(value: str) -> str:
    """Accept only the literal ``env``; never treat argv as the key itself."""
    if value != "env":
        raise argparse.ArgumentTypeError(
            "--monitor-key accepts only 'env'; the key is read from the environment"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--api-base",
        default=os.environ.get("OUTPOST_API_BASE", DEFAULT_API_BASE),
        help=(
            "monitor API base URL (default: OUTPOST_API_BASE or "
            f"{DEFAULT_API_BASE})"
        ),
    )
    parser.add_argument(
        "--monitor-key-env",
        default=DEFAULT_MONITOR_KEY_ENV,
        help=(
            "name of the environment variable holding the monitor key "
            f"(default: {DEFAULT_MONITOR_KEY_ENV}); the key itself is never "
            "accepted on the command line"
        ),
    )
    parser.add_argument(
        "--monitor-key",
        dest="monitor_key_mode",
        type=_monitor_key_mode,
        default=None,
        help=(
            "compatibility spelling from the ops docs: pass the literal 'env' "
            "to read the key from the environment (any other value is rejected)"
        ),
    )
    parser.add_argument(
        "--telegram-api-base",
        default=os.environ.get("OUTPOST_TELEGRAM_API_BASE", DEFAULT_TELEGRAM_API_BASE),
        help=f"Telegram Bot API base URL (default: {DEFAULT_TELEGRAM_API_BASE})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one lease -> getMe -> result cycle and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the effective configuration only; no lease, Telegram, or "
            "result call is made"
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help=f"seconds to sleep between cycles (default: {DEFAULT_POLL_SECONDS:.0f})",
    )
    parser.add_argument(
        "--backoff-max-seconds",
        type=float,
        default=DEFAULT_BACKOFF_MAX_SECONDS,
        help=(
            "upper bound for any retry/backoff sleep "
            f"(default: {DEFAULT_BACKOFF_MAX_SECONDS:.0f})"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity (default: INFO)",
    )
    return parser


def _dry_run_report(args: argparse.Namespace) -> None:
    key = os.environ.get(args.monitor_key_env)
    if key:
        key_state = f"set (masked {mask_token(key)})"
    else:
        key_state = f"missing (a real run would exit 2; export {args.monitor_key_env})"
    print("Dry run: no lease, Telegram, or result calls will be made.")
    print(f"api_base={args.api_base.rstrip('/')}")
    print(f"lease_endpoint={args.api_base.rstrip('/')}{LEASE_PATH}")
    print(f"result_endpoint={args.api_base.rstrip('/')}{RESULT_PATH}")
    print(f"telegram_api_base={args.telegram_api_base.rstrip('/')}")
    print(f"monitor_key_env={args.monitor_key_env} — {key_state}")
    print(f"poll_seconds={args.poll_seconds:g}")
    print(f"backoff_max_seconds={args.backoff_max_seconds:g}")
    print(f"mode={'once' if args.once else 'loop'}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.dry_run:
        _dry_run_report(args)
        return 0

    monitor_key = os.environ.get(args.monitor_key_env, "")
    if not monitor_key:
        print(
            f"ERROR: {args.monitor_key_env} is not set. Export the monitor key "
            "in the environment; it is never accepted as a command-line value.",
            file=sys.stderr,
        )
        return 2
    if args.poll_seconds < 0 or args.backoff_max_seconds < 0:
        print("ERROR: --poll-seconds and --backoff-max-seconds must be >= 0", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_run(args, monitor_key))
    except KeyboardInterrupt:
        LOGGER.info("Interrupted; exiting cleanly")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
