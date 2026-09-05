"""Opt-in, read-only connectivity probes for external scanner providers."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live]

TIMEOUT_PER_SCANNER = 45


async def _run_with_timeout(
    name: str,
    operation: Callable[[], Awaitable[object]],
    timeout: int = TIMEOUT_PER_SCANNER,
) -> object:
    try:
        return await asyncio.wait_for(operation(), timeout=timeout)
    except TimeoutError:
        pytest.fail(f"{name} timed out after {timeout}s")
    except Exception as exc:
        pytest.fail(f"{name} failed: {exc}")


@pytest.mark.asyncio
async def test_all_configured_scanners_are_reachable(monkeypatch):
    if os.getenv("RUN_LIVE_SCANNER_TESTS") != "1":
        pytest.skip("set RUN_LIVE_SCANNER_TESTS=1 to enable external scanner probes")

    from app.workers.tasks import scanner_tasks

    redis_client = MagicMock()
    redis_client.get.return_value = None
    redis_client.set.return_value = True
    monkeypatch.setattr(scanner_tasks, "redis_client", redis_client)

    # A connectivity probe must never persist discovered credentials or enqueue validation.
    monkeypatch.setattr(scanner_tasks, "_save_credentials_async", AsyncMock(return_value=0))

    probes: list[tuple[str, Callable[[], Awaitable[object]], int]] = [
        (
            "GitHub",
            lambda: scanner_tasks._scan_github_async('filename:.env "TELEGRAM_BOT_TOKEN"'),
            45,
        ),
        ("GitLab", scanner_tasks._scan_gitlab_async, 30),
        ("Gist", scanner_tasks._scan_gist_async, 30),
        ("GrepApp", scanner_tasks._scan_grepapp_async, 30),
        ("Pastebin", scanner_tasks._scan_pastebin_async, 20),
        (
            "Serper",
            lambda: scanner_tasks._scan_serper_async(
                'site:pastebin.com "api.telegram.org/bot"'
            ),
            20,
        ),
        (
            "Google",
            lambda: scanner_tasks._scan_google_async(
                'site:pastebin.com "api.telegram.org/bot"'
            ),
            20,
        ),
        ("Bitbucket", scanner_tasks._scan_bitbucket_async, 30),
        ("PublicWWW", scanner_tasks._scan_publicwww_async, 20),
        (
            "URLScan",
            lambda: scanner_tasks._scan_urlscan_async("api.telegram.org/bot"),
            45,
        ),
        (
            "FOFA",
            lambda: scanner_tasks._scan_fofa_async(None, 'body="api.telegram.org/bot"'),
            45,
        ),
        (
            "Shodan",
            lambda: scanner_tasks._scan_shodan_async('http.html:"api.telegram.org/bot"'),
            45,
        ),
        ("Shodan C2", scanner_tasks._scan_shodan_c2_async, 45),
        ("Netlas", scanner_tasks._scan_netlas_async, 45),
    ]

    results = {}
    for name, operation, timeout in probes:
        results[name] = await _run_with_timeout(name, operation, timeout)

    assert set(results) == {name for name, _, _ in probes}
