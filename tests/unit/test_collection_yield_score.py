"""Regression + structural tests for Plan Item 2:
`confidence_score` → `collection_yield_score` rename.

These tests verify the migration file, Python source, and TypeScript frontend
carry the expected patterns without requiring a live Postgres. A separate
integration test (opt-in via ALLOW_SUPABASE_WRITE=1) exercises the migration
against a real database.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    REPO_ROOT / "supabase" / "migrations" / "20260903000003_collection_yield_score.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert MIGRATION_PATH.exists(), f"Missing migration file: {MIGRATION_PATH}"
    return MIGRATION_PATH.read_text(encoding="utf-8")


# ============================================================
# Migration structure — non-destructive rename
# ============================================================
class TestMigrationStructure:
    def test_adds_collection_yield_score_generated_column(self, migration_sql: str):
        # New generated column exists.
        assert re.search(
            r"ADD COLUMN collection_yield_score INTEGER GENERATED ALWAYS AS",
            migration_sql,
        ), "collection_yield_score must be added as a STORED generated column"
        assert "STORED" in migration_sql

    def test_backwards_compatible_meta_fallback(self, migration_sql: str):
        # Generated column falls back to legacy meta key so existing writers
        # keep producing valid scores during transition.
        assert "meta ? 'collection_yield_score'" in migration_sql
        assert "meta ? 'confidence_score'" in migration_sql, (
            "Backwards-compatible fallback to meta->>'confidence_score' required"
        )

    def test_legacy_confidence_score_column_not_dropped(self, migration_sql: str):
        # The rename must be non-destructive: the old column stays as an alias.
        assert not re.search(
            r"DROP\s+COLUMN\s+confidence_score", migration_sql, re.IGNORECASE
        ), "confidence_score must NOT be dropped — it is a backwards-compat alias"

    def test_public_view_exposes_both_columns(self, migration_sql: str):
        # The public view must expose both names during transition.
        view_block = re.search(
            r"CREATE OR REPLACE VIEW public\.discovered_credentials_public AS(.+?);",
            migration_sql,
            re.DOTALL,
        )
        assert view_block, "public view must be redefined"
        body = view_block.group(1)
        assert "confidence_score" in body
        assert "collection_yield_score" in body


# ============================================================
# Migration idempotency — safe to re-run
# ============================================================
class TestMigrationIdempotency:
    def test_uses_if_not_exists_or_existence_guard(self, migration_sql: str):
        # Every DDL that mutates schema must be guarded.
        # Count guarded and unguarded statements to be sure.
        assert migration_sql.count("IF NOT EXISTS") >= 4
        assert migration_sql.count("DO $$") >= 3, (
            "DO-block existence checks required for pg_attribute / pg_constraint / "
            "information_schema guards"
        )

    def test_function_uses_create_or_replace(self, migration_sql: str):
        assert "CREATE OR REPLACE FUNCTION public.calculate_finding_priority" in migration_sql

    def test_drop_function_signature_before_replace(self, migration_sql: str):
        # DROP FUNCTION with signature handles return-type changes on re-run.
        assert re.search(
            r"DROP FUNCTION IF EXISTS public\.calculate_finding_priority",
            migration_sql,
        )


# ============================================================
# finding_summaries new fields
# ============================================================
class TestFindingSummariesFields:
    def test_confidence_column_added(self, migration_sql: str):
        assert re.search(
            r"ADD COLUMN IF NOT EXISTS confidence REAL NOT NULL",
            migration_sql,
        )

    def test_confidence_range_check_constraint(self, migration_sql: str):
        assert "finding_summaries_confidence_range" in migration_sql
        assert re.search(
            r"CHECK\s*\(\s*confidence\s*>=\s*0\.0\s+AND\s+confidence\s*<=\s*1\.0\s*\)",
            migration_sql,
        ), "confidence must be bounded to [0, 1] via CHECK constraint"

    def test_explanation_column_added_not_null(self, migration_sql: str):
        assert re.search(
            r"ADD COLUMN IF NOT EXISTS explanation TEXT NOT NULL",
            migration_sql,
        )

    def test_severity_bucket_check_in_create_table(self, migration_sql: str):
        # Table create (for cold envs) must carry the severity bucket check.
        assert "severity IN ('low','medium','high','critical')" in migration_sql

    def test_priority_bounded_1_to_10(self, migration_sql: str):
        assert re.search(r"priority BETWEEN 1 AND 10", migration_sql)


# ============================================================
# calculate_finding_priority() — deterministic function
# ============================================================
class TestCalculateFindingPriority:
    def test_function_is_immutable(self, migration_sql: str):
        # IMMUTABLE == same inputs => same outputs. Postgres will not cache
        # a non-IMMUTABLE function this way, so this label is our determinism
        # contract at the SQL layer.
        fn_block = re.search(
            r"CREATE OR REPLACE FUNCTION public\.calculate_finding_priority.+?\$\$;",
            migration_sql,
            re.DOTALL,
        )
        assert fn_block, "function definition not found"
        body = fn_block.group(0)
        assert "IMMUTABLE" in body

    def test_returns_severity_priority_explanation(self, migration_sql: str):
        assert re.search(
            r"RETURNS TABLE\s*\(\s*severity\s+VARCHAR\(16\)\s*,\s*priority\s+INTEGER\s*,\s*explanation\s+TEXT\s*\)",
            migration_sql,
        )

    def test_priority_clamped_to_valid_range(self, migration_sql: str):
        # priority must be clamped to [1, 10] to satisfy the finding_summaries CHECK.
        assert "GREATEST(LEAST(v_priority, 10), 1)" in migration_sql

    def test_confidence_clamped_to_unit_interval(self, migration_sql: str):
        # confidence input must be clamped to [0, 1] for determinism on bad input.
        assert "GREATEST(LEAST(COALESCE(p_confidence, 0.0), 1.0), 0.0)" in migration_sql

    def test_severity_bands_are_deterministic(self, migration_sql: str):
        # All four severity buckets must be reachable through CASE-style logic.
        for level in ("critical", "high", "medium", "low"):
            assert f"'{level}'" in migration_sql, f"severity band '{level}' missing"


# ============================================================
# Python source — validator writes both meta keys during transition
# ============================================================
class TestValidatorWritesBothMetaKeys:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return (REPO_ROOT / "app" / "workers" / "tasks" / "validation_tasks.py").read_text(
            encoding="utf-8"
        )

    def test_update_path_writes_collection_yield_score(self, source: str):
        # Existing-row update path must write the new key alongside the alias.
        assert 'merged_meta["collection_yield_score"] = confidence_score' in source

    def test_update_path_still_writes_legacy_key(self, source: str):
        assert 'merged_meta["confidence_score"] = confidence_score' in source, (
            "Legacy key retained during rename transition"
        )

    def test_new_record_insert_writes_both_keys(self, source: str):
        assert '"collection_yield_score": confidence_score' in source
        assert '"confidence_score": confidence_score' in source

    def test_backfill_writes_both_keys(self, source: str):
        # Backfill task must produce meta with both keys so it does not
        # re-queue itself endlessly under either query filter.
        assert '"collection_yield_score": score' in source
        assert '"confidence_score": score' in source

    def test_backfill_query_matches_missing_new_or_legacy_key(self, source: str):
        # Match rows missing EITHER key so we backfill legacy rows too.
        assert (
            'meta->>collection_yield_score.is.null,meta->>confidence_score.is.null'
            in source
        )


# ============================================================
# Python source — monitor API accepts new sort key
# ============================================================
class TestMonitorApiAllowsNewSortKey:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return (REPO_ROOT / "app" / "api" / "routers" / "monitor.py").read_text(
            encoding="utf-8"
        )

    def test_collection_yield_score_in_allowed_sorts(self, source: str):
        # Both keys must be in the whitelist for backwards-compat + new callers.
        assert '"collection_yield_score"' in source
        assert '"confidence_score"' in source

    def test_migration_fallback_handles_new_column_name(self, source: str):
        # The migration-not-applied catch must also handle the new name.
        assert '"collection_yield_score" in msg' in source


# ============================================================
# Frontend — Credential type + preferred column
# ============================================================
class TestFrontendCredentialType:
    @pytest.fixture(scope="class")
    def page_source(self) -> str:
        return (REPO_ROOT / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def sidebar_source(self) -> str:
        return (REPO_ROOT / "frontend" / "components" / "Sidebar.tsx").read_text(
            encoding="utf-8"
        )

    def test_credential_type_declares_new_field(self, page_source: str):
        assert "collection_yield_score?: number | null;" in page_source

    def test_credential_type_keeps_legacy_field_deprecated(self, page_source: str):
        # The legacy field must remain (backwards-compat) but be marked deprecated.
        assert "confidence_score?: number | null;" in page_source
        assert "@deprecated" in page_source

    def test_sidebar_selects_both_columns(self, sidebar_source: str):
        assert "collection_yield_score" in sidebar_source
        assert "confidence_score" in sidebar_source

    def test_sidebar_prefers_new_column_for_badge(self, sidebar_source: str):
        # ConfidenceBadge should read the new column with legacy fallback.
        assert "cred.collection_yield_score ?? cred.confidence_score" in sidebar_source
