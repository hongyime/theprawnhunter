"""Check a watchlist pattern against the stored corpus before enabling it.

Reports how many historical messages match and shows samples, so a rule can be
sanity-checked for false positives (names, order ids, phone numbers) without
trusting a translation.

Usage:
    python scripts/ops/validate_watchlist_pattern.py "kumarhane|kumar oyna"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def project_ref(supabase_url: str) -> str:
    host = supabase_url.strip().rstrip("/").split("://", 1)[-1]
    return host.split(".")[0]


def run_query(sql: str) -> list[dict]:
    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
    url = os.environ.get("SUPABASE_URL", "")
    if not token or not url:
        raise SystemExit("SUPABASE_ACCESS_TOKEN and SUPABASE_URL must be set")
    request = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{project_ref(url)}/database/query",
        data=json.dumps({"query": sql}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode() or "[]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pattern", help="Postgres/Python regular expression")
    parser.add_argument("--samples", type=int, default=5, help="sample rows to show")
    args = parser.parse_args(argv)
    load_env()

    literal = args.pattern.replace("'", "''")
    try:
        counts = run_query(
            "select count(*) as total, count(*) filter (where content ~* '"
            f"{literal}') as matched from public.exfiltrated_messages"
        )
        samples = run_query(
            "select left(content, 120) as sample, created_at::date as day "
            "from public.exfiltrated_messages where content ~* '"
            f"{literal}' order by created_at desc limit {int(args.samples)}"
        )
    except urllib.error.HTTPError as exc:
        print(f"query failed: HTTP {exc.code} {exc.read().decode()[:300]}")
        return 1

    total = counts[0]["total"] if counts else 0
    matched = counts[0]["matched"] if counts else 0
    print(f"corpus={total} matched={matched}")
    for row in samples:
        print(f"  {row['day']}  {row['sample']!r}")
    if matched == 0:
        print("No historical hits: safe to enable, but it produces no signal yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
