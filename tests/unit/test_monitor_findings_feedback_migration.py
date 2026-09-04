"""Service-only monitor feedback RPC contract."""

from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "supabase"
    / "migrations"
    / "20260904000005_monitor_findings_feedback.sql"
)


def test_monitor_feedback_is_transactional_audited_and_service_only():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION public.record_finding_feedback_service" in sql
    assert "INSERT INTO public.finding_feedback" in sql
    assert "UPDATE public.findings" in sql
    assert "INSERT INTO public.audit_logs" in sql
    assert "IF NOT FOUND" in sql
    assert "FROM PUBLIC, anon, authenticated" in sql
    assert "TO service_role" in sql
