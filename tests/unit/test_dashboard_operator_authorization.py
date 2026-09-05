"""Structural guard for dashboard authorization beyond mere authentication."""

from __future__ import annotations

from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def test_dashboard_surfaces_require_admin_controlled_operator_claim():
    sql = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(MIGRATIONS.glob("*.sql"))
    )

    assert "CREATE OR REPLACE FUNCTION public.is_dashboard_operator()" in sql
    operator_guard = sql[sql.rindex("CREATE OR REPLACE FUNCTION public.is_dashboard_operator()") :]
    assert "auth.jwt()" in operator_guard
    assert "app_metadata" in operator_guard
    assert "operator" in operator_guard

    # The helper must guard the three queue tables, both redacted dashboard
    # views, and the feedback RPC rather than merely checking auth.uid().
    assert operator_guard.count("public.is_dashboard_operator()") >= 7
