"""Structural checks for graph and owned-bot funnel persistence."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "20260904000003_entities_engagement.sql"
BOT_LISTENER = ROOT / "app" / "services" / "bot_listener.py"
CELERY = ROOT / "app" / "workers" / "celery_app.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_typed_graph_has_integrity_provenance_and_stable_upserts():
    sql = _read(MIGRATION)
    assert "CREATE TABLE IF NOT EXISTS public.entities" in sql
    assert "CREATE TABLE IF NOT EXISTS public.entity_edges" in sql
    assert "REFERENCES public.entities(id) ON DELETE RESTRICT" in sql
    assert "source_entity_id <> target_entity_id" in sql
    assert "evidence_source_table TEXT NOT NULL" in sql
    assert "evidence_source_id TEXT NOT NULL" in sql
    assert "first_seen_at TIMESTAMPTZ NOT NULL" in sql
    assert "last_seen_at TIMESTAMPTZ NOT NULL" in sql
    assert "confidence REAL NOT NULL" in sql
    assert "provenance JSONB NOT NULL" in sql
    assert "ON CONFLICT (entity_type, canonical_value) DO UPDATE" in sql
    assert "ON CONFLICT (edge_key) DO UPDATE" in sql
    assert "CREATE OR REPLACE FUNCTION public.upsert_entity_edges_batch" in sql


def test_owned_bot_funnel_contract_has_all_stages_and_180_day_expiry():
    sql = _read(MIGRATION)
    assert "CREATE TABLE IF NOT EXISTS public.engagement_events" in sql
    assert "CREATE OR REPLACE VIEW public.engagement_funnel_daily" in sql
    for event_type in (
        "start",
        "first_inbound",
        "qualified",
        "handoff",
        "outcome",
        "opt_out",
        "block_report",
    ):
        assert f"'{event_type}'" in sql
    assert "p_occurred_at + INTERVAL '180 days'" in sql
    assert "ON CONFLICT (event_key) DO UPDATE" in sql
    conflict_clause = sql[sql.index("ON CONFLICT (event_key) DO UPDATE") :]
    assert "occurrence_count + 1" not in conflict_clause


def test_graph_and_funnel_are_authenticated_read_service_write_only():
    sql = _read(MIGRATION)
    for table in ("entities", "entity_edges", "engagement_events"):
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"REVOKE ALL ON public.{table} FROM PUBLIC, anon, authenticated" in sql
        assert f"GRANT SELECT ON public.{table} TO authenticated" in sql
        assert f"GRANT ALL ON public.{table} TO service_role" in sql
    assert "GRANT EXECUTE ON FUNCTION public.upsert_engagement_event" in sql
    assert "TO service_role" in sql


def test_only_owned_monitor_bot_handlers_feed_the_funnel():
    listener = _read(BOT_LISTENER)
    assert "track_owned_bot_start(update, context)" in listener
    assert "track_owned_bot_first_inbound(update, context)" in listener
    assert "track_owned_bot_opt_out(update, context)" in listener
    assert 'CommandHandler(["stop", "optout", "unsubscribe"]' in listener
    assert "filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND" in listener
    assert "private message content is never persisted" in listener


def test_graph_builder_is_scheduled_as_a_bounded_idempotent_job():
    celery = _read(CELERY)
    assert '"build-entity-graph-hourly"' in celery
    assert '"task": "flow.build_entity_graph"' in celery
    assert '"credential_limit": 2000' in celery
    assert '"evidence_limit": 50000' in celery
