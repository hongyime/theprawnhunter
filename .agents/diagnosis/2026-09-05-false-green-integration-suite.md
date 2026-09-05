# False-green integration suite

**Date:** 2026-09-05  
**Status:** Confirmed  
**Scope:** Pytest collection and live integration probes

## Symptom contract

The default test suite must either execute a test or report an intentional skip. An enabled live
probe must fail when its operation raises, times out, cannot authenticate, or cannot verify its
result. It must not mutate production services unless the operator explicitly opted in.

## Evidence

Running `.venv-test\Scripts\python.exe -m pytest` collected 383 tests and reported `381 passed,
2 skipped`, but also emitted `PytestUnhandledCoroutineWarning` for:

- `tests/integration/test_broadcaster.py::test_broadcaster`
- `tests/integration/test_scanners.py::test_all`

Those functions are `async def` without an asyncio marker, so pytest silently skips them. The
broadcaster probe would also send five real messages to `@theprawnhunter` and catches every send
exception without failing. The scanner probe describes timeouts as successes and exits from inside
an async test instead of asserting its result.

`tests/test_supabase_rw.py::test_rw` is counted as passed when `ALLOW_SUPABASE_WRITE` or Supabase
configuration is absent because it returns normally. It also catches every database exception,
uses a fixed token hash, and does not guarantee cleanup if verification fails.

## Root cause

Standalone operational scripts were named as pytest tests without a declared live-test contract.
Missing pytest markers, normal returns for unmet prerequisites, and broad exception handling turned
"not executed" and "failed" outcomes into a green suite.

## Fix

1. Register a `live` pytest marker and make the asyncio loop scope explicit.
2. Gate side-effecting tests behind narrowly named opt-in environment variables and call
   `pytest.skip` when a prerequisite is absent.
3. Mark async probes with `pytest.mark.asyncio`; fail on exceptions and timeouts.
4. Use unique database test data, verify the round trip, and clean it up in `finally`.
5. Add harness-integrity tests so async tests and live probes cannot regress to implicit skips.

## Verification plan

- Run the focused harness tests and the three repaired integration modules.
- Run the complete pytest suite and confirm there is no `PytestUnhandledCoroutineWarning`.
- Run enabled live probes only against explicitly configured services; verify failures propagate.
- Run frontend typecheck/Vitest, E2E smoke checks, a bounded health-endpoint load test, and the
  operational health scripts before the release gate.

## Recurrence control

Keep strict marker validation enabled. The harness-integrity test statically rejects unmarked async
pytest functions and live probes that use a normal return as their skip path. Live mutations remain
opt-in and isolated from the default deterministic suite.
