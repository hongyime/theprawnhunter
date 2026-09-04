"""Structural contract for policy-routed finding alerts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "20260904000004_finding_alert_policies.sql"
CONFIG = ROOT / "app" / "core" / "config.py"
FLOW = ROOT / "app" / "workers" / "tasks" / "flow_tasks.py"
CELERY = ROOT / "app" / "workers" / "celery_app.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_policy_schema_has_routes_quiet_hours_claims_and_audit():
    sql = _read(MIGRATION)
    assert "CREATE TABLE IF NOT EXISTS public.finding_alert_policies" in sql
    assert "CREATE TABLE IF NOT EXISTS public.finding_alert_deliveries" in sql
    assert "CREATE TABLE IF NOT EXISTS public.finding_alert_audit" in sql
    for value in ("immediate", "daily", "weekly", "telegram", "webhook"):
        assert f"'{value}'" in sql
    assert "timezone TEXT NOT NULL" in sql
    assert "quiet_start TIME" in sql
    assert "quiet_end TIME" in sql
    assert "finding_alert_delivery_once UNIQUE" in sql
    assert "CREATE OR REPLACE FUNCTION public.claim_finding_alert" in sql
    assert "CREATE OR REPLACE FUNCTION public.defer_finding_alert" in sql
    assert "CREATE OR REPLACE FUNCTION public.complete_finding_alert" in sql


def test_alert_tables_are_authenticated_read_and_service_write_only():
    sql = _read(MIGRATION)
    for table in (
        "finding_alert_policies",
        "finding_alert_deliveries",
        "finding_alert_audit",
    ):
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"REVOKE ALL ON public.{table} FROM PUBLIC, anon, authenticated" in sql
        assert f"GRANT SELECT ON public.{table} TO authenticated" in sql
        assert f"GRANT ALL ON public.{table} TO service_role" in sql


def test_raw_stream_is_default_off_and_policy_jobs_are_scheduled():
    config = _read(CONFIG)
    flow = _read(FLOW)
    celery = _read(CELERY)
    assert "ENABLE_RAW_MESSAGE_BROADCAST: bool = False" in config
    assert "ENABLE_LEGACY_EVENT_ALERTS: bool = False" in config
    assert "if not settings.ENABLE_RAW_MESSAGE_BROADCAST" in flow
    for task in (
        "flow.route_finding_deltas",
        "flow.daily_findings_digest",
        "flow.weekly_finding_alerts",
    ):
        assert task in flow
        assert task in celery
