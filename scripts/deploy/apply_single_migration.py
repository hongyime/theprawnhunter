"""Apply a Supabase migration via the Management API.

Reads the Supabase Personal Access Token from SUPABASE_ACCESS_TOKEN and the
project ref from SUPABASE_URL (https://<ref>.supabase.co). The SQL is sent to
the Management API query endpoint, which executes multi-statement scripts.

Usage:
    SUPABASE_ACCESS_TOKEN=sbp_... python scripts/deploy/apply_single_migration.py \\
        supabase/migrations/20261024000002_discovered_credentials_public_chat_fields.sql

If the request times out mid-backfill, re-run the command — the backfill
procedure commits every 500 rows, so it resumes from the last committed batch.
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


def project_ref_from_url(supabase_url: str) -> str:
    url = supabase_url.strip().rstrip("/")
    scheme = url.split("://", 1)
    host = scheme[1] if len(scheme) == 2 else url
    return host.split(".")[0]


def load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def apply_migration(supabase_url: str, token: str, sql: str) -> tuple[int, str]:
    ref = project_ref_from_url(supabase_url)
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    body = json.dumps({"query": sql}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("migration_file", help="path to the .sql migration to apply")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request target and SQL length without calling the API",
    )
    args = parser.parse_args(argv)

    load_env()
    sql_path = Path(args.migration_file)
    if not sql_path.is_file():
        print(f"ERROR: {sql_path} not found", file=sys.stderr)
        return 2
    sql = sql_path.read_text()

    supabase_url = os.environ.get("SUPABASE_URL", "")
    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "")

    if args.dry_run:
        if not supabase_url:
            print("ERROR: SUPABASE_URL not set (check .env)", file=sys.stderr)
            return 2
        if not token:
            print(
                "Dry run: SUPABASE_ACCESS_TOKEN not set — export it to apply for real.",
                file=sys.stderr,
            )
        print(
            f"Dry run: would POST {len(sql)} chars of SQL to project "
            f"ref '{project_ref_from_url(supabase_url)}'"
        )
        return 0

    if not token:
        print(
            "ERROR: SUPABASE_ACCESS_TOKEN is not set. Create one in the Supabase "
            "dashboard (Account → Access Tokens) and export it.",
            file=sys.stderr,
        )
        return 2
    if not supabase_url:
        print("ERROR: SUPABASE_URL not set (check .env)", file=sys.stderr)
        return 2

    status, body = apply_migration(supabase_url, token, sql)
    if 200 <= status < 300:
        print(f"OK: migration applied (HTTP {status}).")
        return 0
    short = " ".join(body.split())[:300]
    print(f"FAIL: HTTP {status} — {short}", file=sys.stderr)
    print(
        "The Management API executes the whole script in one transaction, so a "
        "failure rolls everything back. Fix the SQL and re-run; the script is "
        "idempotent.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
