#!/usr/bin/env python
"""Fetch the production operational report from the API."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request


def _load_dotenv_if_needed() -> None:
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _get_json(url: str, monitor_key: str | None = None) -> dict:
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/json")
    if monitor_key:
        request.add_header("X-Monitor-Key", monitor_key)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    _load_dotenv_if_needed()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.getenv("API_BASE_URL") or f"http://127.0.0.1:{os.getenv('API_PORT', '8011')}",
    )
    parser.add_argument("--monitor-key", default=os.getenv("MONITOR_API_KEY"))
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    if not args.monitor_key:
        print(json.dumps({"status": "blocked", "reason": "MONITOR_API_KEY is required"}, indent=2))
        return 2

    try:
        health = _get_json(f"{base_url}/health/")
        operational = _get_json(f"{base_url}/health/operational", args.monitor_key)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "api_http_error",
                    "status_code": exc.code,
                    "body": body,
                },
                indent=2,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "reason": "api_unreachable", "error": str(exc)[:500]},
                indent=2,
            )
        )
        return 1

    report = {"health": health, "operational": operational}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if operational.get("status") == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
