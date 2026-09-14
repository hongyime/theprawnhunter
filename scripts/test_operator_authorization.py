"""Exercise database authorization with synthetic rows in an empty local database.

No application imports or network providers. PGDATABASE must start with
prawn_hunter_auth_fixture_; PGHOST must be loopback. PSQL selects the client.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "supabase" / "tests" / "operator_authorization"
ACTOR = "11111111-1111-4111-8111-111111111111"
FINDING = "22222222-2222-4222-8222-222222222222"


def sql(query: str) -> str:
    result = subprocess.run(
        [os.environ.get("PSQL", "psql"), "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
        input=query, capture_output=True, text=True, encoding="utf-8", timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def session(claims: dict, query: str, role: str = "authenticated") -> str:
    assert role in {"authenticated", "anon", "service_role"}
    encoded = json.dumps(claims).replace("'", "''")
    return sql(
        f"BEGIN; SET LOCAL ROLE {role}; "
        f"SET LOCAL request.jwt.claims = '{encoded}'; {query}; ROLLBACK;"
    )


def counts() -> str:
    return """SELECT json_build_array(
        (SELECT count(*) FROM public.findings),
        (SELECT count(*) FROM public.finding_feedback),
        (SELECT count(*) FROM public.finding_evidence),
        (SELECT count(*) FROM public.evidence_redacted))"""


def run() -> None:
    assert os.environ.get("PGHOST") in {"127.0.0.1", "::1", "localhost"}
    assert re.fullmatch(r"prawn_hunter_auth_fixture_[a-z0-9_]+", os.environ["PGDATABASE"])
    assert sql("SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace") == "0"
    sql((FIXTURES / "baseline.sql").read_text(encoding="utf-8"))
    ordinary = {"role": "authenticated", "sub": ACTOR}
    operator = {**ordinary, "app_metadata": {"operator": True}}
    revoked = {**ordinary, "app_metadata": {"operator": False}}
    # Reproduce all four flaws first; the fixture must not start already secure.
    assert session(revoked, "SELECT public.is_dashboard_operator()") == "t"
    assert json.loads(session(ordinary, counts())) == [0, 0, 1, 1]
    assert session(ordinary, f"SELECT public.record_finding_feedback('{FINDING}', 'synthetic')")
    print("PASS: baseline reproduces false-claim, policy, view and RPC bypasses", flush=True)

    migration = ROOT / "supabase/migrations/20260912121145_restrict_dashboard_operator_access.sql"
    sql("BEGIN; " + migration.read_text(encoding="utf-8") + " COMMIT;")

    denied = [
        ("missing claim", ordinary),
        ("null metadata", {**ordinary, "app_metadata": None}),
        ("user-editable metadata", {**ordinary, "user_metadata": {"operator": True}}),
        ("missing subject", {"role": "authenticated", "app_metadata": {"operator": True}}),
        ("missing role", {"sub": ACTOR, "app_metadata": {"operator": True}}),
        ("anon role claim", {**operator, "role": "anon"}),
        ("service role claim", {**operator, "role": "service_role"}),
    ]
    for value in [False, None, "true", "false", 1, 0, [], {}, [True]]:
        denied.append((f"operator {value!r}", {**ordinary, "app_metadata": {"operator": value}}))
    for name, claims in denied:
        assert session(claims, "SELECT public.is_dashboard_operator()") == "f", name
        assert json.loads(session(claims, counts())) == [0, 0, 0, 0], name
        # Catch the exception inside a subtransaction; verify no writes escaped.
        query = f"""DO $test$ BEGIN
          BEGIN
            PERFORM public.record_finding_feedback('{FINDING}', 'denied', p_status=>'changed');
            RAISE EXCEPTION 'Expected denial';
          EXCEPTION WHEN insufficient_privilege THEN NULL;
                    WHEN raise_exception THEN
                      IF SQLERRM <> 'Authentication required' THEN RAISE; END IF;
          END;
        END $test$;
        RESET ROLE;
        SELECT json_build_array(
          (SELECT count(*) FROM public.finding_feedback),
          (SELECT count(*) FROM public.audit_logs),
          (SELECT status FROM public.findings WHERE id='{FINDING}'))"""
        assert json.loads(session(claims, query)) == [1, 0, "open"], name
        print(f"PASS: denied helper, tables, view and mutation: {name}", flush=True)

    assert json.loads(session(operator, counts())) == [1, 1, 1, 1]
    positive = f"""SELECT public.record_finding_feedback(
      '{FINDING}', 'synthetic', p_status=>'reviewed', p_assignee=>'fixture');
      RESET ROLE;
      SELECT json_build_array(
        (SELECT count(*) FROM public.finding_feedback),
        (SELECT count(*) FROM public.audit_logs),
        (SELECT status FROM public.findings WHERE id='{FINDING}'),
        (SELECT assignee FROM public.findings WHERE id='{FINDING}'),
        (SELECT actor_id::text FROM public.finding_feedback WHERE label='synthetic'))"""
    assert json.loads(session(operator, positive).splitlines()[-1]) == [2, 1, "reviewed", "fixture", ACTOR]
    print("PASS: boolean true retains only the existing authorized contract", flush=True)

    missing = "55555555-5555-4555-8555-555555555555"
    failed_write = f"""DO $test$ BEGIN
      BEGIN
        PERFORM public.record_finding_feedback('{missing}', 'missing fixture');
        RAISE EXCEPTION 'Expected missing finding failure';
      EXCEPTION WHEN foreign_key_violation THEN NULL;
      END;
    END $test$;
    RESET ROLE;
    SELECT json_build_array(
      (SELECT count(*) FROM public.finding_feedback),
      (SELECT count(*) FROM public.audit_logs))"""
    assert json.loads(session(operator, failed_write)) == [1, 0]
    print("PASS: missing finding rolls back feedback and audit writes", flush=True)

    # A later permissive policy must not undo the restrictive table guards.
    sql("CREATE POLICY fixture_future_allow ON public.finding_evidence FOR SELECT TO authenticated USING (true)")
    assert json.loads(session(ordinary, counts())) == [0, 0, 0, 0]
    assert json.loads(session(operator, counts())) == [1, 1, 1, 1]
    print("PASS: extra permissive policy cannot bypass the restriction", flush=True)

    for role in ("anon", "authenticated"):
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert sql(f"SELECT has_table_privilege('{role}','public.evidence_redacted','{privilege}')") == "f"
        assert sql(f"SELECT has_table_privilege('{role}','public.exfiltrated_messages','SELECT')") == "f"
    assert sql("SELECT has_table_privilege('anon','public.evidence_redacted','SELECT')") == "f"
    for signature in ("is_dashboard_operator()", "record_finding_feedback(uuid,text,text,text,text,text,text)"):
        assert sql(f"SELECT has_function_privilege('anon','public.{signature}','EXECUTE')") == "f"
    assert sql("SELECT prosecdef FROM pg_proc WHERE oid='public.is_dashboard_operator()'::regprocedure") == "f"
    assert sql("SELECT reloptions @> ARRAY['security_barrier=true'] FROM pg_class WHERE oid='public.evidence_redacted'::regclass") == "t"
    assert session({}, "SELECT count(*) FROM public.findings", "service_role") == "1"
    print("PASS: raw reads, anonymous execution and view writes denied; service table access unchanged", flush=True)


if __name__ == "__main__":
    run()
