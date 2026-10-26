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


RESULT_ROWS = ".result-item, .result, table tbody tr, .result-list > *"


def _has_results(page) -> bool:
    try:
        return page.locator(RESULT_ROWS).count() > 0
    except Exception:  # noqa: BLE001
        return False


def _needs_attention(page) -> str | None:
    if _has_results(page):
        return None
    for selector in ("#captcha", ".verify-wrap", ".nc-container", "iframe[src*='captcha']"):
        try:
            if page.locator(selector).count() > 0:
                return "captcha"
        except Exception:  # noqa: BLE001
            pass
    for selector in ("form[action*='/login']", ".login-form", "#login-form"):
        try:
            if page.locator(selector).count() > 0:
                return "login"
        except Exception:  # noqa: BLE001
            pass
    return "results-missing"


def _wait_for_results(page, seconds: int = 25) -> bool:
    try:
        page.wait_for_selector(RESULT_ROWS, timeout=seconds * 1000)
        return True
    except Exception:  # noqa: BLE001
        return False


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
                "meta": {
                    "source": "fofa_browser",
                    "query": query,
                    "context": (row.get("context") or "")[:300],
                    **({"url": row["url"]} if row.get("url") else {}),
                },
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


def _result_urls(page, limit: int) -> list[str]:
    """Return de-duplicated http(s) URLs exposed by the current result rows."""
    urls: list[str] = []
    try:
        anchors = page.locator(".hsxa-meta-data-item a[href^='http']")
        for index in range(min(anchors.count(), limit * 4)):
            href = anchors.nth(index).get_attribute("href") or ""
            if href and href not in urls and "fofa.info" not in href:
                urls.append(href)
            if len(urls) >= limit:
                break
    except Exception:  # noqa: BLE001
        pass
    return urls


def _sweep_host(url: str, timeout: float) -> list[dict]:
    """Best-effort fetch of a result host, swept for tokens."""
    import urllib.error
    import urllib.request

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2_000_000).decode("utf-8", errors="ignore")
    except Exception as error:  # noqa: BLE001 - one host must not stop the run
        print(f"[fofa] host fetch skipped {url}: {type(error).__name__}")
        return []
    return [
        {
            "token": hit["token"],
            "chat_id": hit.get("chat_id"),
            "context": hit.get("context", ""),
            "url": url,
        }
        for hit in sweep_text_for_tokens(body, "fofa_host", max_bytes=2_000_000)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument(
        "--cdp",
        default=os.environ.get("FOFA_CDP_URL"),
        help="attach to a running Chrome (e.g. http://127.0.0.1:9222); never closes it",
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help="close the launched browser when finished (default: leave it open)",
    )
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument("--login-wait", type=int, default=300, help="seconds to wait for a manual login/captcha solve")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--fetch-hosts",
        type=int,
        default=0,
        help="also fetch the first N result URLs and sweep their bodies for tokens",
    )
    parser.add_argument("--fetch-timeout", type=float, default=6.0)
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    profile = Path(args.profile).expanduser()
    if not profile.is_dir():
        print(f"profile not found: {profile}")
        return 2

    total = 0
    with sync_playwright() as pw:
        browser = None
        context = None
        reused = False
        if args.cdp:
            browser = pw.chromium.connect_over_cdp(args.cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            existing = [p for p in context.pages if "fofa.info" in (p.url or "")]
            if existing:
                page = existing[0]
                reused = True
                print(f"[fofa] attached to {args.cdp}, reusing the open FOFA tab")
            else:
                page = context.pages[0] if context.pages else context.new_page()
                print(f"[fofa] attached to {args.cdp}, no FOFA tab found; using an existing tab")
        else:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=args.headless,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1440, "height": 900},
            )
        if not args.cdp:
            page = context.new_page()
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
                _wait_for_results(page)
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
                if args.fetch_hosts:
                    seen = {row["token"] for row in results}
                    urls = _result_urls(page, args.fetch_hosts)
                    print(f"[fofa] fetching {len(urls)} host page(s)")
                    for host_url in urls:
                        for row in _sweep_host(host_url, args.fetch_timeout):
                            if row["token"] not in seen:
                                seen.add(row["token"])
                                results.append(row)
                print(f"[fofa] query {query!r}: {len(results)} token(s)")
                total += len(results)
                if results and not args.dry_run:
                    print(f"[fofa] ingest response: {_post(results, query)}")
                _jitter(4, 10)
        finally:
            if not reused:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
            if not args.cdp and args.close:
                context.close()
            elif not args.cdp:
                print("[fofa] browser left open (pass --close to close it)")

    print(f"[fofa] done, {total} token(s) found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
