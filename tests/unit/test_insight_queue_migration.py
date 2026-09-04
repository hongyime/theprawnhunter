"""Structural checks for the Insight Queue migration and worker wiring."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "20260904000002_insight_queue.sql"
FLOW_TASKS = ROOT / "app" / "workers" / "tasks" / "flow_tasks.py"
CELERY = ROOT / "app" / "workers" / "celery_app.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_exact_queue_tables_and_v1_types_are_constrained():
    sql = _read(MIGRATION)
    assert "CREATE TABLE IF NOT EXISTS public.findings" in sql
    assert "CREATE TABLE IF NOT EXISTS public.finding_feedback" in sql
    assert "ALTER TABLE public.finding_evidence" in sql
    for finding_type in (
        "credential_exposure",
        "infrastructure_cluster",
        "cross_bot_pattern",
    ):
        assert f"'{finding_type}'" in sql
    for column in (
        "canonical_key",
        "why_it_matters",
        "recommended_action",
        "score_explanation",
        "evidence_count",
        "source_table",
        "source_id",
        "excerpt_redacted",
        "provenance",
    ):
        assert re.search(rf"\b{column}\b", sql)


def test_legacy_rows_are_backfilled_and_evidence_fk_moves_without_deletion():
    sql = _read(MIGRATION)
    statements = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
    assert "FROM public.finding_summaries AS summary" in sql
    assert "UPDATE public.finding_evidence" in sql
    assert "REFERENCES public.findings(id) ON DELETE CASCADE" in sql
    assert "DROP TABLE" not in statements.upper()
    assert not re.search(r"\bDELETE\s+FROM\b", statements, re.IGNORECASE)


def test_producer_rpc_is_atomic_batched_and_idempotent():
    sql = _read(MIGRATION)
    assert "CREATE OR REPLACE FUNCTION public.upsert_finding(" in sql
    assert "CREATE OR REPLACE FUNCTION public.upsert_findings_batch(" in sql
    assert "ON CONFLICT (type, canonical_key) DO UPDATE" in sql
    assert "LEAST(public.findings.first_seen_at" in sql
    assert "GREATEST(public.findings.last_seen_at" in sql
    assert "ON CONFLICT (finding_id, evidence_key) DO UPDATE" in sql
    assert "p_candidates is limited to 250 findings per call" in sql


def test_queue_and_feedback_are_authenticated_only():
    sql = _read(MIGRATION)
    for table in ("findings", "finding_evidence", "finding_feedback"):
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"REVOKE ALL ON public.{table} FROM PUBLIC, anon, authenticated" in sql
        assert f"GRANT SELECT ON public.{table} TO authenticated" in sql
    assert "acting_user UUID := auth.uid()" in sql
    assert "RAISE EXCEPTION 'Authentication required'" in sql
    assert "GRANT EXECUTE ON FUNCTION public.record_finding_feedback" in sql


def test_feedback_is_append_only_audited_and_updates_disposition_atomically():
    sql = _read(MIGRATION)
    function = sql[sql.index("CREATE OR REPLACE FUNCTION public.record_finding_feedback") :]
    assert function.index("INSERT INTO public.finding_feedback") < function.index(
        "UPDATE public.findings"
    )
    assert "INSERT INTO public.audit_logs" in function
    assert "'finding.feedback'" in function
    assert "GRANT ALL ON public.finding_feedback TO authenticated" not in sql


def test_existing_reports_persist_findings_and_never_broadcast_raw_user_ids():
    flow = _read(FLOW_TASKS)
    assert '@app.task(name="flow.produce_findings")' in flow
    assert "infrastructure_cluster_candidates" in flow
    assert "cross_bot_pattern_candidates" in flow
    assert "pseudonymize_subject(uid)" in flow
    assert 'f"• `user:{uid}`' not in flow


def test_bounded_idempotent_producer_is_scheduled():
    celery = _read(CELERY)
    assert '"produce-findings-15min"' in celery
    assert '"task": "flow.produce_findings"' in celery
    assert '"credential_limit": 2000' in celery
    assert '"message_limit": 50000' in celery
