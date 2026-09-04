"""Structural safety checks for retention schema and operator cleanup."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "supabase" / "migrations" / "20260903000001_supabase_optimization.sql"
OPERATION = REPO_ROOT / "database" / "operations" / "retention_cleanup.sql"
DISABLE_LEGACY_JOBS = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260904000001_disable_legacy_retention_jobs.sql"
)


def _read(path: Path) -> str:
    assert path.exists(), f"Missing retention file: {path}"
    return path.read_text(encoding="utf-8")


def _without_line_comments(sql: str) -> str:
    return re.sub(r"--.*$", "", sql, flags=re.MULTILINE)


def test_migration_is_schema_only_and_rerunnable():
    sql = _read(MIGRATION)
    statements = _without_line_comments(sql)
    assert not re.search(r"\bDELETE\s+FROM\b", statements, re.IGNORECASE)
    assert not re.search(r"\bTRUNCATE\b", statements, re.IGNORECASE)
    assert not re.search(r"\bVACUUM\b", statements, re.IGNORECASE)
    assert "cron.schedule" not in statements
    assert "CREATE TABLE IF NOT EXISTS public.retention_archive" in sql
    assert "CREATE TABLE IF NOT EXISTS public.retention_cleanup_runs" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql


def test_raw_archive_is_not_exposed_to_frontend_roles():
    sql = _read(MIGRATION)
    assert "ALTER TABLE public.retention_archive ENABLE ROW LEVEL SECURITY" in sql
    for role in ("PUBLIC", "anon", "authenticated"):
        assert f"REVOKE ALL ON public.retention_archive FROM {role}" in sql


def test_operation_defaults_to_no_write_dry_run():
    sql = _read(OPERATION)
    assert re.search(r"v_dry_run\s+BOOLEAN\s*:=\s*TRUE", sql)
    dry_run = sql.index("IF v_dry_run THEN")
    first_archive_write = sql.index("INSERT INTO public.retention_archive")
    first_source_delete = sql.index("DELETE FROM public.")
    assert dry_run < first_archive_write < first_source_delete
    assert "RETURN;" in sql[dry_run:first_archive_write]


def test_execution_requires_exact_confirmation_token():
    sql = _read(OPERATION)
    confirmation = sql.index("v_confirmation IS DISTINCT FROM 'PURGE_ARCHIVED_ROWS'")
    first_archive_write = sql.index("INSERT INTO public.retention_archive")
    assert confirmation < first_archive_write
    assert "RAISE EXCEPTION" in sql[confirmation:first_archive_write]


def test_every_source_delete_requires_matching_current_archive_snapshot():
    sql = _read(OPERATION)
    expected_tables = {
        "telemetry_indicators": "id",
        "finding_evidence": "id",
        "exfiltrated_messages": "id",
        "audit_logs": "id",
        "honeypot_updates": "id",
        "keepalive_log": "id",
        "engagement_events": "id",
        "finding_summaries": "finding_id",
    }
    for table, identifier in expected_tables.items():
        delete = re.search(
            rf"DELETE FROM public\.{table} AS (?P<alias>\w+)(?P<body>.+?);",
            sql,
            re.DOTALL,
        )
        assert delete, f"missing explicit cleanup for {table}"
        alias = delete.group("alias")
        body = delete.group("body")
        assert "USING public.retention_archive AS archive" in body
        assert f"archive.source_table = '{table}'" in body
        assert f"archive.source_id = {alias}.{identifier}::text" in body
        assert f"archive.payload = to_jsonb({alias})" in body


def test_cascade_dependents_are_archived_before_parent_rows():
    sql = _read(OPERATION)
    assert sql.index("SELECT 'telemetry_indicators'") < sql.index(
        "SELECT 'exfiltrated_messages'"
    )
    assert sql.index("SELECT 'finding_evidence'") < sql.index(
        "SELECT 'finding_summaries'"
    )


def test_operation_does_not_install_automatic_scheduling():
    sql = _read(OPERATION)
    assert "cron.schedule" not in sql


def test_forward_migration_disables_every_legacy_cleanup_job_without_purging():
    sql = _read(DISABLE_LEGACY_JOBS)
    statements = _without_line_comments(sql)
    assert "to_regclass('cron.job') IS NULL" in sql
    assert "PERFORM cron.unschedule(legacy_job.jobid)" in sql
    assert "cron.schedule" not in sql
    assert not re.search(r"\bDELETE\s+FROM\b", statements, re.IGNORECASE)
    assert not re.search(r"\bTRUNCATE\b", statements, re.IGNORECASE)
    assert not re.search(r"\bVACUUM\b", statements, re.IGNORECASE)

    expected_jobs = {
        "cleanup-keepalive",
        "cleanup-broadcasted-messages",
        "cleanup-stale-messages",
        "cleanup-audit-logs",
        "cleanup-honeypot-updates",
        "cleanup-telemetry-indicators",
        "cleanup-finding-summaries",
        "cleanup-finding-evidence",
    }
    for job_name in expected_jobs:
        assert f"'{job_name}'" in sql
