---
> Recovery note (added by the orchestrating subagent, not by Codex): Codex completed this audit but could not write this file itself - its sandbox marks .agents read-only.
> The report below is copied verbatim from the Codex log on 3410 (/home/bryan/.codex/log/codex-latent-bugs-audit-20261013.log, session 01a135ac-a64a-7980-9ce7-4c8d4af7eb89, lines 18892-18983). No finding text was changed.
> Codex's Worktree check line saying the report file is absent refers to its sandbox failure and is superseded by this recovery. No code was changed; this report file is the only change.
---

# Latent Bugs Audit — 2026-10-13

Read-only review of application code, scripts, and relevant tests. No tests were run. Ruff was unavailable in the environment.

## CRASH

### Bot replies call the same helper again

**File:** `app/services/_bot_listener/common.py:54`

`_reply_and_delete` calls itself instead of sending a reply. Any command that tries to answer can loop until the bot handler errors, so users may get no response.

**How tests may have missed it:** The visible `test_getfile_rejects_group_chats_before_authorization` reaches this helper without replacing it, so it should catch the problem if run. I can’t confirm why this reached production without running tests or seeing the CI history.

### Login cleanup uses a helper it cannot find

**File:** `app/services/_bot_listener/login.py:112`  
**Other calls:** lines 133, 152, 168, 179, 243, 420, 428, 462, 475

Login calls `_wipe_conversation`, but that helper is defined in the separate `commands.py` file and is not imported into `login.py`. Login errors, cancellation, timeout, and successful cleanup can therefore stop with an error before the messages are wiped.

**How tests may have missed it:** The existing login error test patches the bot-listener facade. That does not add the missing name to `login.py`, where the login handler looks for it. The test should expose the problem if run; I did not run it.

### Login calls a message-deletion helper it cannot find

**File:** `app/services/_bot_listener/login.py:374`  
**Also called at:** line 411

`schedule_deletion` is defined in `commands.py` but is not imported into `login.py`. If saving a login session fails because the file is locked, this call errors. On the invite-message path, the error is caught and the invite message is left without its intended deletion.

**How tests may have missed it:** The available tests do not cover the locked-file or invite-message paths.

## SILENT DATA LOSS

### A failed message batch is skipped while the scrape returns partial results

**File:** `app/services/scraper_srv.py:473–475`

The scraper checks messages in groups. If one group request fails, it logs only a debug message and continues with the other groups. Any messages in the failed group are absent from this scrape. When other groups returned messages, the overall result is treated as successful, so the missed messages may not be retried before they fall outside a later scrape’s range.

**How tests may have missed it:** I found no test for a failed message-ID group; the relevant tests cover successful media retrieval.

### A backfill failure can permanently block another attempt

**File:** `app/workers/tasks/audit_tasks.py:813, 819, 830–834`

The job records each message as handled in Redis before it updates the database. If that database update fails, the job never removes the Redis markers. Later runs skip those messages as already handled, so they may never be requeued. The markers have no expiry.

**How tests may have missed it:** The backfill tests cover successful updates, but not a database failure after the Redis markers have been written.

### The browser extension can discard results that were never uploaded

**File:** `extension/background.js:526–529`  
**Also:** lines 140–146

When the extension reaches its storage limit, it removes older results. It does this whether or not the upload succeeded. So a failed upload—or missing upload settings—followed by a long scan can cause found tokens to disappear before they are saved. The storage-full retry also keeps the full seen-token list while trimming the results.

**How tests may have missed it:** I found no extension test for upload failure followed by the result limit or storage-full cleanup.

### The scanner test can pass when no tokens are queued

**File:** `tests/integration/test_scanner_flow.py:14–17, 46–47`

The test mocks the validator and checks only that the returned count is at least zero. A broken scanner that queues nothing would still pass. Its `requests.get` mock is unused; the real validator uses `httpx` in `app/workers/tasks/validation_tasks.py:176`.

**What could break:** Found tokens might never reach validation or be saved, while this test still passes.

## WRONG BEHAVIOUR

### Failed backfills are recorded as successful jobs

**File:** `app/workers/tasks/audit_tasks.py:830–834`

On failure, the backfill catches the error and returns an error message instead of raising it. The message is logged, but the task system receives a normal return value and can record the job as successful. That can hide the failure from job-status views and prevent automatic retries.

**How tests may have missed it:** The tests cover successful backfills, not how the task system records a failed one.

## Tool stand-in check

The backfill test fake accepts any `.select()` call order. I checked the corresponding live call against the project’s pinned Supabase Python client: its official docs allow `.select()` after an update and its filters, so I did not count that call as a live crash. [Supabase Python update docs](https://supabase.com/docs/reference/python/update)

## Worktree check

Final `git status --short` was empty. No code or other files changed. The report file is absent because `.agents` is read-only in this workspace.

## TOP 5 MOST DANGEROUS FINDINGS

1. **Bot replies can error:** the reply helper calls itself instead of sending a message.
2. **Backfill messages can be lost:** a failed database update leaves permanent markers that block later attempts.
3. **Login cleanup can error:** login calls a cleanup helper it never imports.
4. **Extension results can be discarded:** old results are removed even when upload failed.
5. **Scrape batches can be skipped:** one failed group is silently omitted while other messages make the scrape look successful.
