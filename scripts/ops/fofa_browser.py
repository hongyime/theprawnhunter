"""Scrape FOFA results through a real logged-in Chrome profile.

FOFA's API needs a paid/enabled account and its web UI challenges automation,
so this runner drives a real Chrome profile that you have logged into, with
human-like pacing, and pauses for you to solve a captcha or log in.

Usage:
    python scripts/ops/fofa_browser.py --query 'body="api.telegram.org/bot"'
    python scripts/ops/fofa_browser.py --cdp http://127.0.0.1:9222 --query 'app="telegram"'
    python scripts/ops/fofa_browser.py --profile ~/.config/google-chrome-cdp --login-wait 600

The profile must not be open in another Chrome instance (Chrome locks it).
With --cdp the lane opens its own tab and leaves the browser running.

Exit codes: 0 ok, 2 profile/args error, 3 needs operator action (log in, start
Chrome, or fix the FOFA session). Exit 3 is mirrored to the Telegram System
Logs topic via MONITOR_API_KEY.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
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


PROBE_QUERY = base64.b64encode(b'body="th-lane-probe-nonexistent"').decode()


def _extension_domain(context) -> str:
    """The domain the extension last published (default: cn)."""
    try:
        for cookie in context.cookies():
            if cookie.get("name") == "th_fofa_lane_domain":
                value = cookie.get("value")
                if value in ("cn", "en"):
                    return value
    except Exception:  # noqa: BLE001 - absence just means "unknown/default"
        pass
    return "cn"


def _domain_logged_in(context, domain: str) -> bool | None:
    """Load a real result page and see whether it lands on /f_login.

    A plain HTTP request is not enough: the FOFA SPA redirects client-side.
    """
    probe = None
    try:
        probe = context.new_page()
        probe.goto(
            f"https://{domain}/result?qbase64={PROBE_QUERY}",
            wait_until="commit",
            timeout=30000,
        )
        probe.wait_for_timeout(2500)
        return "/f_login" not in probe.url
    except Exception:  # noqa: BLE001 - unknown, not "logged out"
        return None
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:  # noqa: BLE001
                pass


def _choose_domain(
    extension_domain: str,
    requested: str,
    health: dict[str, bool | None],
) -> str | None:
    """Pick a domain: opposite of the extension by default, else the working one."""
    preferred = (
        requested
        if requested in ("cn", "en")
        else ("en" if extension_domain == "cn" else "cn")
    )
    other = "cn" if preferred == "en" else "en"
    if health.get(preferred) is True:
        return preferred
    if health.get(other) is True:
        return other
    if health.get(preferred) is None:
        return preferred
    if health.get(other) is None:
        return other
    return None


def _notify(message: str, dedupe_key: str | None = None) -> bool:
    """Post to the API so the message lands in the Telegram System Logs topic."""
    token = (getattr(settings, "MONITOR_API_KEY", None) or "").strip()
    base = os.environ.get("FOFA_INGEST_BASE", "http://127.0.0.1:8011").rstrip("/")
    if not token:
        print("[fofa] notify skipped: MONITOR_API_KEY is not configured")
        return False
    payload: dict[str, str] = {"message": message[:3900]}
    if dedupe_key:
        payload["dedupe_key"] = dedupe_key
    request = urllib.request.Request(
        f"{base}/monitor/ops/system-log",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Monitor-Key": token},
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status == 200
        except urllib.error.HTTPError as error:
            # 503 is the broadcaster's 2 s cooldown; retry once, then give up.
            if error.code != 503 or attempt:
                print(f"[fofa] notify failed: HTTP {error.code}")
                return False
        except Exception as error:  # noqa: BLE001 - notify must never break the lane
            if attempt:
                print(f"[fofa] notify failed: {type(error).__name__}")
                return False
        time.sleep(2.5)
    return False


def _token_expiry(context) -> float | None:
    """Return ``fofa_token`` expiry (epoch seconds) or None when absent."""
    try:
        for cookie in context.cookies(["https://fofa.info/"]):
            if cookie.get("name") == "fofa_token":
                return cookie.get("expires")
    except Exception:  # noqa: BLE001 - a cookie read failure must not crash the lane
        return None
    return None


def _session_issue(expires: float | None, warn_days: float, now: float | None = None) -> str | None:
    """Describe why the FOFA session is unusable, or None when it is healthy."""
    if not expires or expires < 0:
        return "FOFA session cookie missing or session-only — log in to FOFA again"
    days_left = (expires - (now if now is not None else time.time())) / 86400
    if days_left <= 0:
        return "FOFA session expired — log in to FOFA again"
    if days_left < warn_days:
        expires_at = datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"FOFA session expires in {max(1, math.ceil(days_left))} day(s) "
            f"({expires_at}) — log in again soon"
        )
    return None


def _is_empty_result(page) -> bool:
    """True when FOFA rendered a legitimate zero-result page.

    A zero-result page has no result rows, so without this check it looks
    identical to a login wall and would stall the lane for the full login wait.
    """
    try:
        text = page.inner_text("body")[:20000]
    except Exception:  # noqa: BLE001 - missing body is handled as "not empty"
        return False
    lowered = text.lower()
    return any(marker in text for marker in ("0 条匹配结果", "0 条独立IP", "暂无数据")) or any(
        marker in lowered
        for marker in ("0 results", "no results", "0 matches", "no data")
    )


def _has_results(page) -> bool:
    try:
        return page.locator(RESULT_ROWS).count() > 0
    except Exception:  # noqa: BLE001
        return False


def _needs_attention(page) -> str | None:
    try:
        if "/f_login" in (page.url or "") or "/login" in (page.url or ""):
            return "login"
    except Exception:  # noqa: BLE001
        pass
    if _has_results(page) or _is_empty_result(page):
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
    try:
        text = page.inner_text("body")[:40000]
    except Exception:  # noqa: BLE001
        text = ""
    if (
        "[-700]" in text
        or '"code":-700' in text
        or "账号无效" in text
        or "account invalid" in text.lower()
    ):
        return "account-invalid"
    if "本月免费额度已用尽" in text or "需要F点" in text:
        return "quota-exhausted"
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
    try:
        import httpx

        with httpx.Client(timeout=timeout, verify=False, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            body = response.text[:2_000_000]
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
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument(
        "--queries-file",
        help="file with one FOFA query per line (# comments allowed)",
    )
    parser.add_argument("--login-wait", type=int, default=300, help="seconds to wait for a manual login/captcha solve")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-notify", action="store_true", help="do not post to the System Logs topic")
    parser.add_argument(
        "--domain",
        choices=("auto", "cn", "en"),
        default="auto",
        help="auto = opposite of the extension's published domain; cn/en = force",
    )
    parser.add_argument(
        "--warn-days",
        type=float,
        default=float(os.environ.get("FOFA_SESSION_WARN_DAYS", "2")),
        help="stop when fofa_token has fewer days left than this",
    )
    parser.add_argument(
        "--fetch-hosts",
        type=int,
        default=0,
        help="also fetch the first N result URLs and sweep their bodies for tokens",
    )
    parser.add_argument("--fetch-timeout", type=float, default=6.0)
    args = parser.parse_args(argv)

    queries = list(args.query)
    if args.queries_file:
        try:
            with open(args.queries_file, encoding="utf-8") as handle:
                queries.extend(
                    line.strip()
                    for line in handle
                    if line.strip() and not line.lstrip().startswith("#")
                )
        except OSError as error:
            print(f"queries file unavailable: {error}")
            return 2
    if not queries:
        parser.error("at least one --query or --queries-file is required")
    args.query = queries

    from playwright.sync_api import sync_playwright

    profile = Path(args.profile).expanduser()
    if not args.cdp and not profile.is_dir():
        print(f"profile not found: {profile}")
        return 2

    total = 0
    completed = 0
    with sync_playwright() as pw:
        if args.cdp:
            try:
                browser = pw.chromium.connect_over_cdp(args.cdp)
            except Exception as error:  # noqa: BLE001 - operator action, not a crash
                message = (
                    f"FOFA CDP Chrome unreachable at {args.cdp} "
                    f"({type(error).__name__}). Lane not started."
                )
                print(f"[fofa] {message}")
                if not args.no_notify:
                    _notify(f"⚠️ {message}", dedupe_key="fofa:browser:cdp-down")
                return 3
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            print(f"[fofa] attached to {args.cdp}")
        else:
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=args.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1440, "height": 900},
                )
            except Exception as error:  # noqa: BLE001 - profile lock is a known manual fix
                message = (
                    f"FOFA Chrome profile unavailable ({profile}): "
                    f"{type(error).__name__}. Close Chrome or use --cdp."
                )
                print(f"[fofa] {message}")
                if not args.no_notify:
                    _notify(f"⚠️ {message}", dedupe_key="fofa:browser:profile-busy")
                return 3

        extension_domain = _extension_domain(context)
        health = {
            "cn": _domain_logged_in(context, "fofa.info"),
            "en": _domain_logged_in(context, "en.fofa.info"),
        }
        chosen = _choose_domain(extension_domain, args.domain, health)
        if chosen is None:
            message = (
                "FOFA CN and EN are both logged out; lane not started. "
                "Log in to at least one domain."
            )
            print(f"[fofa] {message}")
            if not args.no_notify:
                _notify(f"🔒 {message}", dedupe_key="fofa:browser:login-both")
            return 3
        domain = "fofa.info" if chosen == "cn" else "en.fofa.info"
        print(
            f"[fofa] extension domain: {extension_domain}; "
            f"probe cn={health['cn']} en={health['en']}; using {domain}"
        )
        page = context.new_page()

        def session_gate(label: str) -> bool:
            issue = _session_issue(_token_expiry(context), args.warn_days)
            if not issue:
                return True
            print(f"[fofa] {label}: {issue}")
            if not args.no_notify:
                _notify(
                    f"⚠️ FOFA browser lane ({domain}) stopped ({label}).\n{issue}\n"
                    f"queries completed: {completed}, tokens kept: {total}\n"
                    "action: log in to FOFA, then rerun the lane",
                    dedupe_key="fofa:browser:session",
                )
            return False

        try:
            if not session_gate("before run"):
                return 3
            for query in args.query:
                if not session_gate(f"before query {query!r}"):
                    return 3
                encoded = base64.b64encode(query.encode()).decode()
                url = f"https://{domain}/result?qbase64={encoded}"
                print(f"[fofa] opening {url}")
                try:
                    page.goto(url, wait_until="commit", timeout=120000)
                except Exception as error:  # noqa: BLE001 - slow/blocked pages still get a manual window
                    print(f"[fofa] navigation slow ({type(error).__name__}); leaving the window open")
                _jitter()
                _wait_for_results(page)
                _human_scroll(page)

                reason = _needs_attention(page)
                if reason == "quota-exhausted":
                    message = (
                        f"FOFA monthly free quota exhausted on query {query!r}; "
                        f"{completed} query(ies) completed, {total} token(s) kept. "
                        "Recharge F-coins or wait for the monthly reset."
                    )
                    print(f"[fofa] {message}")
                    if not args.no_notify:
                        _notify(f"💳 {message}", dedupe_key="fofa:browser:quota")
                    return 3
                if reason in ("login", "account-invalid"):
                    message = (
                        f"FOFA session wall ({reason}) on query {query!r}; "
                        f"{completed} query(ies) completed, {total} token(s) kept."
                    )
                    print(f"[fofa] {message}")
                    if not args.no_notify:
                        _notify(
                            f"⚠️ {message}\naction: log in to FOFA, then rerun the lane",
                            dedupe_key=f"fofa:browser:{reason}",
                        )
                    return 3
                if reason:
                    print(f"[fofa] {reason} detected — waiting up to {args.login_wait}s for a manual fix.")
                    if not args.no_notify:
                        _notify(
                            f"⚠️ FOFA {reason} detected — waiting for a manual fix (query {query!r})",
                            dedupe_key=f"fofa:browser:{reason}",
                        )
                    deadline = time.time() + args.login_wait
                    while time.time() < deadline and _needs_attention(page):
                        time.sleep(5)
                    remaining = _needs_attention(page)
                    if remaining:
                        message = (
                            f"FOFA still blocked ({remaining}) after {args.login_wait}s; "
                            f"lane stopped with {total} token(s) kept."
                        )
                        print(f"[fofa] {message}")
                        if not args.no_notify:
                            _notify(f"⚠️ {message}", dedupe_key=f"fofa:browser:{remaining}")
                        return 3
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
                completed += 1
                if results and not args.dry_run:
                    print(f"[fofa] ingest response: {_post(results, query)}")
                _jitter(4, 10)
            if not session_gate("after run"):
                return 3
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            if not args.cdp:
                if args.close:
                    context.close()
                else:
                    print("[fofa] browser left open (pass --close to close it)")

    print(f"[fofa] done, {total} token(s) found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
