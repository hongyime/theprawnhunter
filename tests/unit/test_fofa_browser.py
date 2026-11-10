"""Regression checks for the FOFA CDP lane's restart handling."""

import importlib.util
from pathlib import Path


def _fofa_browser_module():
    path = Path(__file__).parents[2] / "scripts/ops/fofa_browser.py"
    spec = importlib.util.spec_from_file_location("fofa_browser_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cdp_connect_retries_a_brief_chrome_restart(monkeypatch):
    fofa_browser = _fofa_browser_module()
    attempts = 0

    def connect(_url):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("Chrome is restarting")
        return "browser"

    monkeypatch.setattr(fofa_browser, "CDP_CONNECT_ATTEMPTS", 3)
    monkeypatch.setattr(fofa_browser, "CDP_CONNECT_RETRY_SECONDS", 0)

    assert fofa_browser._connect_over_cdp(connect, "http://127.0.0.1:9222") == "browser"
    assert attempts == 3
