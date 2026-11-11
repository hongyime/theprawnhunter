# Diagnosis: Session quarantine false positives

Date: 2026-11-12 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

| Field | Observation |
|---|---|
| Expected | A usable Telethon session remains available unless its SQLite data is actually invalid. |
| Observed | Six session files were renamed to `.session.corrupt.<timestamp>` and disappeared from normal discovery. |
| Scope | Six quarantines in the persistent Docker sessions volume; two account stems no longer have a normal `.session` file. |
| Onset | Quarantine timestamps span 2026-10-28 through 2026-11-09, before the 2026-11-11 delivery rollout. |
| Repro | The current helper renames a session for every exception raised while opening/querying SQLite. |

## Evidence

1. The live volume contains six `.corrupt` files, while only four normal `.session` files remain — `find /app/sessions` in the worker container, 2026-11-12.
2. Every quarantined file returns `PRAGMA integrity_check = ok`, has a `sessions` table, and contains one session row — read-only Python SQLite probe in the worker container, 2026-11-12.
3. Quarantined files are excluded from normal discovery because discovery only scans names ending in `.session` — `app/services/_user_agent/session_lifecycle.py:64-74`.
4. `_is_session_file_healthy` catches every `Exception`, labels it corrupt, and atomically renames the source file — `app/services/_user_agent/common.py:113-150`.
5. The helper does not run `PRAGMA integrity_check`, distinguish `sqlite3.OperationalError` (for example a transient lock/busy condition), or preserve the original error type in a machine-readable result — `app/services/_user_agent/common.py:131-149`.
6. Session lifecycle and login code can operate concurrently against the shared external volume; login publishes with `os.replace`, while lifecycle probes copied sessions — `app/services/_bot_listener/login.py:280-350` and `app/services/_user_agent/session_lifecycle.py:358-380`.

## Root Cause

The quarantine helper treats any SQLite access exception as permanent corruption and renames the active `.session` file. That is a destructive false-positive policy: a transient SQLite failure can remove a healthy session from the sender pool. The six preserved files demonstrate this occurred here because they remain valid SQLite Telethon session databases.

The exact original SQLite error is no longer available in retained logs, so the triggering transient condition is not asserted as fact. A lock/busy race is the leading mechanism because the session volume is shared by concurrent login and lifecycle paths, but the prevention must cover every transient SQLite error rather than rely on that inference.

## Why It Was Not Obvious

The `.corrupt` filename and warning message made a failed probe look like evidence of damaged data. The implementation retained the file, but immediately removed it from discovery and did not revalidate it before treating it as lost.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---|---|
| Retry every failure and never quarantine | Small | A genuinely invalid SQLite file repeatedly consumes probes. | No |
| Quarantine only confirmed invalid SQLite files; treat lock/busy/other probe failures as temporary | Small helper change plus focused tests | A transient failure delays one cycle rather than removing a session. | Yes |
| Add a database-backed session store | Large migration | Broad authentication and recovery risk. | No |

## Verification Plan

1. Add focused tests proving a locked SQLite probe does not rename the session.
2. Add a test proving a malformed/non-SQLite file is still quarantined.
3. Run the session lifecycle tests and the focused broadcast/session tests.
4. Rebuild only after reauthentication restores the missing normal session files; inventory must show every restored account as authorized and eligible before it participates in sender rotation.

## Recurrence Guard

The invariant is: *a healthy Telethon session must never be removed from discovery because of a transient probe error*. Encode this in the session-health unit tests and log the error class separately from confirmed integrity failures.
