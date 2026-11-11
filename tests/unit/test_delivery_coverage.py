"""Regression guards for topic-keyed broadcast delivery coverage."""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "supabase" / "migrations" / "20261111000001_delivery_coverage.sql"
RETENTION_TASK = REPO_ROOT / "app" / "workers" / "tasks" / "retention_tasks.py"


def test_fifo_rpc_is_topic_keyed_and_holds_deferred_heads():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "pending_broadcast_fifo" in sql
    assert "PARTITION BY (c.meta->>'topic_id')::bigint" in sql
    assert "head_retry_at IS NULL OR head_retry_at <= now()" in sql
    assert "LEAST(p_per_topic, 20)" in sql
    assert "LEAST(p_limit, 200)" in sql


@pytest.mark.asyncio
async def test_revoked_close_skips_a_topic_with_pending_messages(monkeypatch):
    from app.services import topic_admin_srv

    async def candidates(_limit):
        return [{"id": "cred-1", "topic_id": 44, "meta": {"topic_id": 44}, "pending_count": 1}]

    monkeypatch.setattr(topic_admin_srv, "fetch_revoked_topic_candidates", candidates)
    monkeypatch.setattr(
        topic_admin_srv,
        "get_broadcaster",
        lambda: (_ for _ in ()).throw(AssertionError("pending topic must not close")),
    )

    result = await topic_admin_srv.close_revoked_topics_logic(limit=10, dry_run=False)

    assert result["candidate_count"] == 0
    assert result["closed"] == 0


def test_routine_retention_only_deletes_confirmed_deliveries_and_keeps_pending_90_days():
    source = RETENTION_TASK.read_text(encoding="utf-8")

    assert 'eq("broadcast_status", "sent")' in source
    assert 'RETENTION_PENDING_DAYS", 90' in source
