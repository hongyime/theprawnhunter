"""Scrape FOFA results through a real logged-in Chrome profile.

FOFA's API needs a paid/enabled account and its web UI challenges automation,
so this runner drives a real Chrome profile that you have logged into, with
human-like pacing, and pauses for you to solve a captcha or log in.

Usage:
    python scripts/ops/fofa_browser.py --query 'body="api.telegram.org/bot"'
    python scripts/ops/fofa_browser.py --profile ~/.config/google-chrome-cdp --login-wait 600
    python scripts/ops/fofa_browser.py --query 'app="telegram"' --dry-run

The profile must not be open in another Chrome instance (Chrome locks it).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings
from app.domain.tokens import sweep_text_for_tokens

DEFAULT_PROFILE = os.environ.get(
    "FOFA_BROWSER_PROFILE", str(Path.home() / ".config" / "google-chrome-cdp")
)
RESULTS_SELECTOR = "#result, .result-list, .result-table, [class*='result']"
LOGIN_MARKERS = ("login-form", "login-container", "login-form-wrap")
CAPTCHA_MARKERS = ("verify-wrap", "nc-container", "captcha", "challenge")


def _jitter(low: float = 2.5, high: float = 7.0) -> None:
    time.sleep(random.uniform(low, high))


def _human_scroll(page, rounds: int = 4) -> None:
    for _ in range(rounds):
        page.mouse.wheel(0, random.randint(400, 900))
        time.sleep(random.uniform(0.6, 1.8))


def _needs_attention(page) -> str | None:
    body = page.content().lower()
    if any(marker in body for marker in CAPTCHA_MARKERS):
        return "captcha"
    if any(marker in body for marker in LOGIN_MARKERS):
        return "login"
    if "result" not in body and "fofa" in page.url:
        return "results-missing"
    return None


def _extract(page) -> list[dict]:
    text = page.inner_text("body")
    return sweep_text_for_tokens(text, "fofa_browser")


def _post(results: list[dict], query: str) -> dict:
    token = (getattr(settings, "MONITOR_API_KEY", None) or "").strip()
    base = os.environ.get("FOFA_INGEST_BASE", "http://127.0.0.1:8011").rstrip("/")
    if not token:
        raise SystemExit("MONITOR_API_KEY is not configured")
    payload = {
        "source": "fofa_browser",
        "domain": "fofa.info",
        "query": query,
        "results": [
            {
                "token": row["token"],
                "chat_id": row.get("chat_id"),
                "valid": None,
                "meta": {"source": "fofa_browser", "query": query, "context": row.get("context", "")[:300]},
            }
            for row in results
        ],
    }
    request = urllib.request.Request(
        f"{base}/ingest/extension/credentials",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Monitor-Key": token},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode() or "{}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument("--login-wait", type=int, default=300, help="seconds to wait for a manual login/captcha solve")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    profile = Path(args.profile).expanduser()
    if not profile.is_dir():
        print(f"profile not found: {profile}")
        return 2

    total = 0
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            for query in args.query:
                encoded = base64.b64encode(query.encode()).decode()
                url = f"https://fofa.info/result?qbase64={encoded}"
                print(f"[fofa] opening {url}")
                try:
                    page.goto(url, wait_until="commit", timeout=120000)
                except Exception as error:  # noqa: BLE001 - slow/blocked pages still get a manual window
                    print(f"[fofa] navigation slow ({type(error).__name__}); leaving the window open")
                _jitter()
                _human_scroll(page)

                reason = _needs_attention(page)
                if reason:
                    print(f"[fofa] {reason} detected — complete it in the browser window.")
                    deadline = time.time() + args.login_wait
                    while time.time() < deadline and _needs_attention(page):
                        time.sleep(5)
                    if _needs_attention(page):
                        print("[fofa] still blocked; giving up on this query")
                        continue
                    print("[fofa] continuing after manual step")
                    _jitter(3, 8)

                results = _extract(page)
                print(f"[fofa] query {query!r}: {len(results)} token(s)")
                total += len(results)
                if results and not args.dry_run:
                    print(f"[fofa] ingest response: {_post(results, query)}")
                _jitter(4, 10)
        finally:
            context.close()

    print(f"[fofa] done, {total} token(s) found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
