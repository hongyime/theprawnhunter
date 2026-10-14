# Diagnosis: backfill failures and overlapping runs

Date: 2026-10-14 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

- **Expected:** An apply backfill that hits an error should be visible to Celery as a failed attempt and retried. A retry should still see rows that the failed database update did not change.
- **Observed:** `_backfill_general_messages_async` catches every exception and returns an error string. The current safety test checks that string, then calls the helper a second time itself.
- **Scope:** Any exception raised inside the backfill helper is converted to a normal return. The current task decorator has no per-task retry settings.
- **Onset:** The earlier idempotency change (`0ce213c`, “fix: mark backfill messages after database update”) moved Redis markers until after a successful database update, but left the catch-and-return behavior in place.
- **Repro:** In `tests/unit/test_general_backfill_safety.py`, set `database.fail_next_update = True`, call the apply helper, then call it again. The first call returns text containing “failed”; the second sees the unchanged row and requeues it.

## Evidence

1. The task is registered with a bare `@app.task(name="system.backfill_general_messages")`; no `autoretry_for` or retry policy is attached (`app/workers/tasks/audit_tasks.py:634-650`).
2. The helper catches `Exception`, logs it, and returns `error_msg`, so Celery receives a normal return (`app/workers/tasks/audit_tasks.py:823-826`).
3. The existing regression injects a database update error, expects a returned string, and manually invokes the helper again (`tests/unit/test_general_backfill_safety.py:254-285`).
4. The global Celery config has late acknowledgements and ignores task results by default (`app/workers/celery_app.py:213-235`), but does not configure automatic retries.
5. The backfill is on-demand through `/backfill` or a manual Celery call; it is not listed in the beat schedule (`app/services/_bot_listener/commands.py:301-328`, `app/workers/celery_app.py:285-300`). There is no backfill lock.
6. Each update includes both the selected IDs and `is_broadcasted=True` plus `broadcast_claimed_at IS NULL`, then changes `is_broadcasted` to `False` (`app/workers/tasks/audit_tasks.py:710-731, 818-824`). This blocks two immediate updates to the same pending row.
7. The candidate list is filtered against Redis once before the database update (`app/workers/tasks/audit_tasks.py:797-800`), and Redis markers are added only after that update (`:821-824`). Two runs can both retain the same unmarked candidate. A successful broadcast later sets `is_broadcasted=True` and clears `broadcast_claimed_at` (`app/workers/tasks/flows/broadcast.py:171-183`), so a delayed second run can match its stale candidate again.
8. The general backfill task has no run lock and is not in the beat schedule; it is queued on demand (`app/services/_bot_listener/commands.py:301-328`; `app/workers/celery_app.py:285-565`, with no `system.backfill_general_messages` entry). The normal broadcast task has its own lock (`app/workers/tasks/flows/broadcast.py:423-437`), but that lock does not cover backfill selection or updates.

## Root Cause

The exception handler turns a failed apply into a successful return, and the task has no automatic retry policy. For overlap, the per-row database condition blocks immediate contention, but it is only a temporary state check. Both runs can pass the Redis check before either marker is written, and the row can become eligible again after broadcast. There is no backfill-level lock to stop the stale second run.

## Why It Was Not Obvious

The helper-level test proved that a later manual invocation could pick up the row, but it asserted the returned error text instead of exercising Celery's task result and retry behavior.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---|---|
| Return an error string | No source change | Celery treats it as success | Reject |
| Re-raise and configure bounded Celery retries | Backfill task and focused regression | Repeats transient failures a bounded number of times | Recommended |
| Add a distributed lock for apply runs | Redis lock around the real apply helper, with a bounded lease and overlap test | A second simultaneous apply is skipped while the first run owns the lock | Recommended |

## Verification Plan

Run the focused backfill safety tests through the registered Celery task. Inject one database update error, confirm Celery stores `RETRY`, and confirm the retry processes the still-eligible row. Start two apply runs with a shared lock backend, hold the first inside its real helper, and confirm the second is skipped and cannot issue a database update.

## Recurrence Guard

Keep a regression that uses the real task and backfill helper. It must fail if a database error is returned as successful text or if the retry cannot find the unchanged row.
