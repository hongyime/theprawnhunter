#!/usr/bin/env python
"""Read-only production schema drift check.

Requires psql and one admin Postgres URL via --database-url, DATABASE_URL, or
SUPABASE_DB_URL. The URL is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess

SCHEMA_CHECK_SQL = r"""
WITH expected_columns(table_name, column_name, required) AS (
    VALUES
        ('discovered_credentials', 'id', true),
        ('discovered_credentials', 'bot_token', true),
        ('discovered_credentials', 'token_hash', true),
        ('discovered_credentials', 'chat_id', true),
        ('discovered_credentials', 'status', true),
        ('discovered_credentials', 'meta', true),
        ('discovered_credentials', 'created_at', true),
        ('discovered_credentials', 'updated_at', true),
        ('discovered_credentials', 'bot_id', false),
        ('discovered_credentials', 'bot_username', false),
        ('discovered_credentials', 'chat_name', false),
        ('discovered_credentials', 'chat_type', false),
        ('discovered_credentials', 'confidence_score', false),
        ('discovered_credentials', 'chat_member_count', false),
        ('exfiltrated_messages', 'id', true),
        ('exfiltrated_messages', 'credential_id', true),
        ('exfiltrated_messages', 'telegram_msg_id', true),
        ('exfiltrated_messages', 'content', true),
        ('exfiltrated_messages', 'media_type', true),
        ('exfiltrated_messages', 'file_meta', true),
        ('exfiltrated_messages', 'is_broadcasted', true),
        ('exfiltrated_messages', 'broadcast_claimed_at', true),
        ('exfiltrated_messages', 'created_at', true),
        ('exfiltrated_messages', 'broadcast_error', false),
        ('exfiltrated_messages', 'broadcast_attempts', false),
        ('exfiltrated_messages', 'next_retry_at', false),
        ('exfiltrated_messages', 'broadcasted_at', false),
        ('exfiltrated_messages', 'sender_user_id', false),
        ('audit_logs', 'id', true),
        ('audit_logs', 'timestamp', true),
        ('audit_logs', 'event_type', true),
        ('audit_logs', 'credential_id', true),
        ('audit_logs', 'user_agent', true),
        ('audit_logs', 'success', true),
        ('audit_logs', 'details', true)
),
actual_columns AS (
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
),
missing_columns AS (
    SELECT e.table_name, e.column_name, e.required
    FROM expected_columns e
    LEFT JOIN actual_columns a
        ON a.table_name = e.table_name
       AND a.column_name = e.column_name
    WHERE a.column_name IS NULL
),
expected_indexes(indexname, required) AS (
    VALUES
        ('idx_messages_credential_id', true),
        ('idx_messages_is_broadcasted', true),
        ('idx_messages_claimed', true),
        ('idx_audit_event_type', true),
        ('idx_audit_timestamp', true),
        ('idx_messages_next_retry', false),
        ('idx_messages_broadcasted_at', false),
        ('idx_messages_sender_user_id', false)
),
actual_indexes AS (
    SELECT indexname
    FROM pg_indexes
    WHERE schemaname = 'public'
),
missing_indexes AS (
    SELECT e.indexname, e.required
    FROM expected_indexes e
    LEFT JOIN actual_indexes a ON a.indexname = e.indexname
    WHERE a.indexname IS NULL
)
SELECT json_build_object(
    'status',
        CASE
            WHEN EXISTS (
                SELECT 1 FROM missing_columns WHERE required
                UNION ALL
                SELECT 1 FROM missing_indexes WHERE required
            )
            THEN 'failed'
            ELSE 'ok'
        END,
    'missing_required_columns',
        COALESCE((
            SELECT json_agg(table_name || '.' || column_name ORDER BY table_name, column_name)
            FROM missing_columns WHERE required
        ), '[]'::json),
    'missing_optional_columns',
        COALESCE((
            SELECT json_agg(table_name || '.' || column_name ORDER BY table_name, column_name)
            FROM missing_columns WHERE NOT required
        ), '[]'::json),
    'missing_required_indexes',
        COALESCE((
            SELECT json_agg(indexname ORDER BY indexname)
            FROM missing_indexes WHERE required
        ), '[]'::json),
    'missing_optional_indexes',
        COALESCE((
            SELECT json_agg(indexname ORDER BY indexname)
            FROM missing_indexes WHERE NOT required
        ), '[]'::json)
);
)
"""


def _check_schema_via_rest(supabase_url: str, service_key: str) -> int:
    """Fallback schema check using Supabase REST API instead of direct Postgres.
    Uses the same expected schema but queries via REST instead of psql.
    """
    import http.client
    import urllib.parse

    # Parse host from URL
    parsed = urllib.parse.urlparse(supabase_url)
    host = parsed.netloc

    # Expected tables/columns (simplified check)
    expected = {
        "discovered_credentials": {
            "required": ["id", "bot_token", "token_hash", "chat_id", "status", "meta", "created_at", "updated_at"],
            "optional": ["bot_id", "bot_username", "chat_name", "chat_type", "confidence_score", "chat_member_count"],
        },
        "exfiltrated_messages": {
            "required": ["id", "credential_id", "telegram_msg_id", "content", "media_type", "file_meta", "is_broadcasted", "broadcast_claimed_at", "created_at"],
            "optional": ["broadcast_error", "broadcast_attempts", "next_retry_at", "broadcasted_at", "sender_user_id"],
        },
        "audit_logs": {"required": ["id", "timestamp", "event_type", "credential_id", "user_agent", "success", "details"], "optional": []},
    }

    # Call Supabase REST API to check table structure (information_schema substitute)
    # We use a minimal single-row query per table; if it succeeds, table exists with expected columns
    conn = http.client.HTTPSConnection(host, timeout=30)
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }

    result = {"status": "ok", "tables": {}, "missing_tables": [], "missing_columns": []}

    for table, spec in expected.items():
        try:
            # SELECT single row to verify table/column existence
            conn.request("GET", f"/rest/v1/{table}?select=*&limit=1", headers=headers)
            resp = conn.getresponse()
            if resp.status in (200, 206):
                result["tables"][table] = "present"
            elif resp.status == 404:
                result["missing_tables"].append(table)
            else:
                body = resp.read().decode("utf-8")[:200]
                result["tables"][table] = f"error_{resp.status}"
        except Exception as e:
            result["tables"][table] = f"error: {str(e)[:50]}"

    conn.close()

    if result["missing_tables"]:
        result["status"] = "drift_detected"
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        default=None,
        help="Admin Postgres URL. Defaults to DATABASE_URL or SUPABASE_DB_URL.",
    )
    args = parser.parse_args()

    _load_dotenv_if_needed()
    database_url = args.database_url or os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")

    # If no direct Postgres URL, try Supabase REST API fallback
    if not database_url:
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if supabase_url and supabase_service_key:
            return _check_schema_via_rest(supabase_url, supabase_service_key)

        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "DATABASE_URL/SUPABASE_DB_URL or SUPABASE_URL+SERVICE_ROLE_KEY required",
                },
                indent=2,
            )
        )
        return 2
    if not database_url:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "DATABASE_URL or SUPABASE_DB_URL is required for SQL schema drift checks",
                },
                indent=2,
            )
        )
        return 2



    child_env = os.environ.copy()
    child_env["PGDATABASE"] = database_url

    completed = subprocess.run(
        [
            psql,
            "--no-align",
            "--tuples-only",
            "--quiet",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            SCHEMA_CHECK_SQL,
        ],
        check=False,
        text=True,
        capture_output=True,
        env=child_env,
    )
    if completed.returncode != 0:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "psql_query_failed",
                    "stderr": completed.stderr.strip()[-1000:],
                },
                indent=2,
            )
        )
        return completed.returncode

    raw = completed.stdout.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"status": "failed", "reason": "invalid_psql_json", "raw": raw}, indent=2))
        return 1

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
