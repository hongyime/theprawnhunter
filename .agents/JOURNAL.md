# theprawnhunter — JOURNAL

## 2026-09-16 — Baseline batch C review

Actor: automated maintenance (OhMyOpenCode / Sisyphus-Junior).

### Actions
- Ran `git fetch origin`, `git status`, `git log --oneline -10` — working tree clean, main up to date.
- Inventoried repo: confirmed Python + Next.js frontend split, 10-service Docker Compose stack, Vercel-linked frontend.
- Queried GitHub: 0 open issues, 0 open PRs on hongyime/theprawnhunter.
- Investigated the "unlinked frontend source" concern:
  - Verified `.vercel/project.json` exists at both repo root and `frontend/.vercel/project.json` — both point to the same Vercel project (`prj_uhn1QEFSTzn33kI2FFXpscgyqt6n`).
  - Confirmed `.vercel/` is in `.gitignore` — neither file is tracked; both are per-checkout `vercel link` artifacts.
  - `frontend/vercel.json` is authoritative and correct.
  - Conclusion: no repo-side fix required. Workflow guidance: run `vercel` only from `frontend/`.
- Created `.agents/STATE.md` and `.agents/JOURNAL.md` (this file) — first-time seed.
- Wrote summary JSON to `X:\01 REPOSITORIES\audit_results\maintenance-2026-09-09\baseline-batch-c-theprawnhunter-20260916.json`.

### No code changes
- No source, config, or workflow files were modified in this session.
- No branches created, nothing pushed, no PRs opened.
- Rationale: no clear low-risk fix surfaced; scope explicitly excludes major refactors; Vercel deployment hold in force until 2026-09-16T07:14:05Z.

### Gaps noted
- No root-level `AGENTS.md`. Creating one would require product-owner input on conventions, so deferred.

### Next candidate work (not executed)
- Author `AGENTS.md` with branch/PR/commit conventions once repo owner confirms style.
- Consider removing the stray root `.vercel/` directory on maintainer workstations to prevent accidental deploy-from-wrong-dir.


## 2026-09-23 — Supabase storage investigation (DB-side only, no app code touched)

Actor: automated maintenance.

Project finjklyfedduvtzqjqad at 277MB/500MB free tier (55%). Checked table
sizes: exfiltrated_messages 128MB + audit_logs 103MB + telemetry_indicators
21MB = ~85% of total. Checked index usage via pg_stat_user_indexes:

- Dropped idx_messages_broadcasted_at (1.9MB, 0 scans ever) via
  DROP INDEX CONCURRENTLY -- safe, no data touched, trivially recreatable.
- Found idx_messages_content_trgm (51MB, only 4 scans total -- ~19% of the
  ENTIRE database for a barely-used fuzzy-search feature) and
  idx_messages_sender_trgm (5.9MB, 4 scans). NOT dropped -- these support an
  actual search capability (rare use != unused), so this is the owner's call,
  not an automated one. Flagged to owner directly.
- audit_logs: 249,820 rows, 95% older than 7 days, 0 older than 30 days (only
  24 days of history exist). Classic operational-log growth, distinct from
  the exfiltrated_messages/findings research data. No retention policy exists
  yet. Flagged to owner as a candidate for a time-based retention window --
  did not implement without explicit sign-off given the project's established
  "never delete records" posture elsewhere; this would need an explicit
  decision on whether audit logs specifically are exempt from that policy.

## 2026-09-24 — Follow-up: owner authorized full action on both flagged items

Owner said "take all actions" on both items flagged above. Executed:

- Dropped idx_messages_content_trgm (51MB, 4 scans) and idx_messages_sender_trgm
  (5.9MB, 4 scans) via DROP INDEX CONCURRENTLY. Fuzzy content/sender search on
  exfiltrated_messages is no longer index-accelerated -- recreatable with
  `CREATE INDEX CONCURRENTLY ... USING gin (content gin_trgm_ops)` if that
  search capability is needed again.
- audit_logs: deleted 203,769 rows older than 14 days (of 250,238 total), then
  VACUUM ANALYZE to actually reclaim the freed pages. Enabled pg_cron
  extension (was not previously installed) and scheduled job
  `audit_logs_retention_14d` (id=1) to run daily at 03:00 UTC, same 14-day
  cutoff, so this does not silently re-accumulate. exfiltrated_messages
  (the actual research data) was NOT touched -- only the operational log table.

Result: DB 277MB -> 222MB (55% -> 44% of the 500MB free-tier cap).

2026-09-27: Runtime, development-launcher, and publishing enhancements were withdrawn after safety review. Only privacy and defensive maintenance remain in scope. No image build, runtime deployment, publication, commit, or push was performed.
- 2026-09-27 22:43:10 +08:00 [PRAWN-E14/claude/stop] branch=maintenance/prawn-ui-20260916 head=5094299 dirty=3
- 2026-09-27 22:43:10 +08:00 [PRAWN-E14/claude/stop] branch=maintenance/prawn-ui-20260916 head=5094299 dirty=3

## 2026-09-27 (continued) — Docker Compose minimal-footprint reorg + git reconciliation

Actor: automated maintenance (OhMyOpenCode / Sisyphus).

### Context
User requested scaling back to dev-only work (no full prod run), minimal Docker resource footprint even in prod, single-stack consistency, a git pull-main-and-merge-to-main-if-needed pass, and a check for stack corruption after they manually deleted all theprawnhunter containers.

### Actions
- Corrected the stale `.agents/STATE.md` claim of "Open PRs: 1" -- verified via GitHub API that PR #21 (Prawn UI visual style) was merged 2026-09-17T23:08:16Z (merge commit `a85decc`) and is already in `main`. 0 open issues, 0 open PRs currently. The local branch `maintenance/prawn-ui-20260916` has been reused for follow-up chores (Supabase storage cleanup, this reorg) after PR #21's original purpose concluded -- this is expected multi-session branch reuse, not drift.
- Found a large uncommitted working-tree diff already present on `docker-compose.yml` (344 lines) and `docker-compose.prod.yml` (35 lines) from a prior/concurrent session: consolidates 4 split celery workers into 1 combined `worker`, moves `bot`/`flower`/`frontend` behind opt-in Compose `profiles`, adds `x-logging`/`x-app-build` anchors, and roughly halves every service's CPU/memory limits. Reviewed it in full before continuing -- it is pure declarative YAML (no image build, no runtime action), well-commented, and matches exactly the "minimal footprint, dev-first, single-stack" reorganization the user asked for. Distinguished it from the separate 2026-09-27 "withdrawn after safety review" note above (that note concerns a DIFFERENT scope -- "runtime, development-launcher, and publishing enhancements" -- not this Compose declarative reorg; confirmed no image build/runtime/publish/commit/push had occurred for either).
- Also found duplicate uncommitted edits to `design.md` and `docs/plans/2026-05-26-scanner-source-expansion.md` (privacy-placeholder anonymization of the maintainer's name) that turned out to be IDENTICAL in intent to a commit already merged into `main` (`6f9179b docs: anonymize optional personal references (#22)`, part of the 9 commits `main` has that this branch does not). Discarded the redundant local copies via `git checkout --` rather than risk a merge conflict re-doing already-landed work; the canonical version will arrive via the upcoming merge from `main`.
- Validated the docker-compose reorg with `docker compose config --quiet` (base), `--profile full config --quiet`, and the prod-overlay + full-profile combination -- all exit 0, confirming the YAML is syntactically and referentially valid without starting anything.
- Checked live Docker state before and after this review: `docker ps -a` shows zero containers (theprawnhunter's stack is indeed fully down, matching the user's report), `docker network ls` shows no `theprawnhunter_net` (consistent with a clean `down`), and all 4 required EXTERNAL volumes (`telegramhunter_redis_data`/`_sessions`/`_imports`/`_beat_schedule`) are still present and untouched -- external volumes are not removed by `docker compose down`/container deletion by design, so there is no data-loss corruction to rectify. One unrelated, auto-named container (`ecstatic_thompson`, image hash `35d01a69f9fd`, running an ad-hoc `apt-get` shell command) was observed once on this shared multi-project machine; it was not part of theprawnhunter's stack (wrong image, no `theprawnhunter_*` name) and had already exited/been removed on its own by the next check -- left untouched throughout, no action taken on it.
- 2026-09-28 08:59:14 +08:00 [PRAWN-E14/claude/stop] branch=maintenance/prawn-ui-20260916 head=7fce95a dirty=2
- 2026-09-28 10:22:10 +08:00 [PRAWN-E14/claude/stop] branch=maintenance/prawn-ui-20260916 head=492362f dirty=0
- 2026-09-28 10:30:11 +08:00 [PRAWN-E14/claude/stop] branch=maintenance/prawn-ui-20260916 head=492362f dirty=0
- 2026-09-28 15:20:23 +08:00 [PRAWN-E14/claude/stop] branch=maintenance/prawn-ui-20260916 head=adf8690 dirty=0
- 2026-09-28 16:30:54 +08:00 [PRAWN-E14/claude/stop] branch=maintenance/prawn-ui-20260916 head=adf8690 dirty=0
- 2026-09-28 19:09:55 +08:00 [PRAWN-E14/claude/stop] branch=maintenance/prawn-ui-20260916 head=b04c778 dirty=0
- 2026-09-28 20:34:43 +08:00 [PRAWN-E14/claude/stop] branch=main head=efee9c2 dirty=0
- 2026-09-28 20:34:43 +08:00 [PRAWN-E14/claude/stop] branch=main head=efee9c2 dirty=0
- 2026-09-28 22:08:20 +08:00 [PRAWN-E14/claude/stop] branch=main head=2d5becb dirty=0

## 2026-10-13 23:12 SGT — redirect error fix resumed (supervisor checkpoint)

- Resumed from disk: HEAD 789887a, fix already on disk in app/workers/tasks/flows/honeypot.py + tests/unit/test_honeypot_redirect_bugs.py. Fix verified by reading the diff: Telegram response body kept on non-success, HTTP status saved, error built as "HTTP <status>: <error text>", "unknown" fallback removed from DB update and failure log.
- Tests re-run by supervisor (repo .venv): 9 passed. Earlier unconfirmed report now confirmed.
- Next: commit + push, rebuild worker only, 10-minute live-log watch (redirect failures should drop or carry a real reason).

## 2026-10-13 23:15 SGT — 5-fix job starting (gate cleared)

- Gate cleared: redirect error fix landed as 5a370ed. 5-fix Codex run starting now; no other Codex job on this repo.
- Earlier 22:58 gate-state block was overwritten by the redirect job's own STATE.md commit (5a370ed) — re-applied. Lesson recorded: commit shared state promptly.
- Supervision (Bryan 2026-10-13): small steps one at a time; STATE.md + JOURNAL.md after EACH step; liveness checks every few minutes; resume from checkpoint on crash, never restart from zero.

## 2026-10-13 23:15 SGT — bot reply helper fix (#1)

- In `app/services/_bot_listener/common.py:54`, changed `_reply_and_delete` to call `update.message.reply_text(text, **kwargs)` instead of recursively calling itself. This lets Telegram commands send their reply and keeps the existing auto-delete scheduling intact.
- Verified before the change: the existing telemetry tests failed with `RecursionError` at this line. After the change, the two focused tests exercising the real helper passed. A broader two-file run also hit two unrelated existing failures: an admin-message assertion mismatch and a missing `_wipe_conversation` import.

## 2026-10-13 23:20 SGT — backfill retry fix (#2)

- In `app/workers/tasks/audit_tasks.py:813-824`, moved Redis handled-marker writes until after the database update succeeds, and mark only rows returned by that update. If the update raises, the outer error handler returns while the messages remain unmarked and eligible for a retry.
- Added a focused failure-then-retry regression in `tests/unit/test_general_backfill_safety.py`; all 8 tests in that file pass.

## 2026-10-13 23:27 SGT — login cleanup imports (#3)

- In `app/services/_bot_listener/login.py:4`, imported `_wipe_conversation` and `schedule_deletion` from their existing owner, `app/services/_bot_listener/commands.py` (definitions at lines 341 and 330). Login referenced `_wipe_conversation` in the cleanup, cancel, and timeout paths, and `schedule_deletion` for delayed final-login notices, without either name being bound in `login.py`; `common.py` does not export them. The focused test reproduced `NameError: name '_wipe_conversation' is not defined` at the first cleanup call.
- Updated `tests/unit/test_bot_listener_security.py` so the failing login path exercises the real wipe helper, and added a real-helper check for delayed deletion using a fake Telegram API boundary. `.venv/bin/pytest -q tests/unit/test_bot_listener_security.py` passes (3 tests).

## 2026-10-13 23:33 SGT — extension result retention (#4)

- In `extension/background.js:136-154` and `:519-594`, storage-full cleanup and result-cap pruning now attempt the API upload first and remove old results only when the response satisfies the existing `res.ok` success check. Missing API settings, HTTP errors, exhausted retries, and timeouts leave results available for another upload attempt; the 300-result target and 50-result storage retry target are unchanged.

## 2026-10-13 23:38 SGT — scraper batch failure reporting (#5)

- In `app/services/scraper_srv.py:307-324` and `:473-490`, a failed Telethon `GetMessages` batch now emits a warning with chat, batch number, ID range, and exception type, then propagates the exception. The existing `MessageIdReader` classifies it as a failed attempt; when its existing rules say the error is retryable, the overall partial scrape stays retryable while keeping messages already collected. The surrounding error log redacts the bot token. Existing tests do not exercise this batch helper; syntax check is used per task instructions. No container rebuild was run; the supervisor handles it.

## 2026-10-13 ~23:50 SGT - rebuild after all 5 fixes (supervisor)

- Rebuilt ONLY worker + bot with base compose (`docker compose --profile full up -d --build --no-deps worker bot`) - base compose (NOT prod overlay) because the running worker had --concurrency=2 (base default; .env sets no override; prod overlay would have changed it to 6).
- Both containers recreated, healthy, RestartCount 0. beat/api/redis/cloudflared untouched.
- 6-min log scan: zero tracebacks / RecursionError / NameError / ImportError / CRITICAL in worker or bot. Worker tasks processing (broadcasts succeeding); bot polling with watchdog.
- Known operational note (not a crash): all 4 Telegram user sessions on cooldown until ~2026-10-14 06:01 UTC; broadcaster falls back to trusting saved topic IDs and sends succeed.

## 2026-10-14 00:05 SGT — 12-scanner check + harden job starting (supervisor checkpoint)

- Gate checked: 5-fix job complete (HEAD d2b0494, worker + bot healthy, no new crashes). No Codex job running on this repo. Other Codex jobs on other repos (unifiedcollector, forge) do not block this repo.
- Job: check and harden all 12 bot-discovery scanners, one at a time, one commit each, in the order listed in STATE.md. Bryan said: continue all the way.
- Rules: read code first; every failure logged clearly, no crash of the whole scan, work stays retryable next time; tests use REAL code; no port / database / login changes; scan times unchanged.
- Next: Scanner 1 — GitHub search.

## Login cleanup import audit — completed

- Confirmed `login.py` already imports `schedule_deletion` from `.commands`; commit `cb8ed1c` introduced the import. The reported issue is already fixed, so no code change, tests, or commit were made.

## 2026-10-14 00:11 SGT — S1.1 GitHub search vet

- Read the GitHub worker, provider, and shared helpers. Network failures and HTTP errors become clear search errors (`app/infrastructure/scanners/providers/github.py:222-239,381-397`); rate replies are detected and given a wait (`:182-220`); empty results count as success (`:280-281`). The worker logs failed searches, keeps them in Redis, carries on after ordinary errors, and stops after a rate limit (`app/workers/tasks/scanners/providers/github.py:108-163`). Missing login is clearly reported (`:93-99`; provider `:253-258`). It asks for 100 results and runs no more than 10 searches with at least 6.1 seconds between them (`:51,58-64,83,162-163`; provider `:267`). The weak points were that separate runs shared no rate timer, and repo/file fetches could keep sending queued requests after a rate reply (`app/infrastructure/scanners/providers/github.py:116-128,285-288,364-375`). GitHub's current docs set code search to 10 requests per minute and 100 results per page; S1.2 fixed these gaps. S1.1 checkpoint commit: `4fa330f`.

## 2026-10-14 00:11 SGT — S1.2 GitHub search hardening

- Added a shared Redis timer so code searches from separate workers stay at least 6.1 seconds apart. A rate reply now sets a shared wait. Repo and file fetch groups stop sending new requests as soon as one sees an error, with no more than three already-started requests in that group. Failures still stay in the retry set and are only removed after a search and save succeed. The 10-search run cap and scan schedule are unchanged. S1.1 checkpoint: `4fa330f`. S1.2 checkpoint commit: `8c9c888`.

## 2026-10-14 00:20 SGT — S1.3 GitHub search tests

- The focused test file passed: 20 passed, 0 failed. The GitHub tests call the real provider and worker, with only GitHub replies and Redis faked. They cover provider outage, HTTP errors 403/404/422/500/503, 429 and both 403 rate limits, empty results, missing login, shared pacing, stopping queued file requests after a rate limit, and a failed search continuing and running again on the next scan. S1.2 checkpoint: `8c9c888`. S1.3 checkpoint commit: `b067399`.

## 2026-10-14 00:24 SGT — S1.4 GitHub search code ready

- Kept the earlier GitHub edits that report provider errors, save failed searches for the next scan, cap and space searches, and report a missing login. Added a shared Redis timer and pause, stopped queued repo/file requests after an error, and saved a supplied search when login is missing. Removed the earlier URLScan response-header fixture edit from the shared test file, and replaced the fake GitHub worker service test with a real provider and worker path. The focused file passes 20 tests with 0 failures. The 4h/6h/12h scan schedule is unchanged. S1.1 checkpoint: `4fa330f`; S1.2: `8c9c888`; S1.3: `b067399`. The S1.4 code commit hash will be added in the completion tick.

## 2026-10-14 00:25 SGT — S1.4 GitHub search complete

- Pushed the GitHub scanner code, STATE.md, and JOURNAL.md as `ca24bc0`. The code commit includes the 20 passing tests. S1.1 checkpoint: `4fa330f`; S1.2: `8c9c888`; S1.3: `b067399`; S1.4 code: `ca24bc0`. This commit records the final S1.4 checkbox tick.


## 2026-10-14 — S2.1 GitHub firehose vet

- Read the full firehose task and the exact token, result, log, and Redis helpers it uses. Event request errors are logged as warnings and stop the page loop (`app/workers/tasks/firehose_tasks.py:88-111`); an HTTP error logs only its status (`:99-101`). An empty event list stops with no special message, then the task logs a normal poll-complete line (`:113-114,217-220`).
- Each commit SHA is marked as seen for seven days before its fetch (`app/workers/tasks/firehose_tasks.py:145-152`). A non-200 commit reply is skipped without a log (`:154-157`); fetch or JSON errors are only logged at debug level (`:168-170`). The code then moves on to other commits, but the failed commit is skipped on later polls because its seen mark remains. The event marker is saved after the loop whether all commit fetches worked or not (`:217-218`).
- Event HTTP errors, including a rate-limit reply, have no special wait or backoff; a commit rate-limit reply is quietly skipped. Commit fetches have a 0.2-second gap (`:171`), the poll reads at most three pages by default (`:39,89`), and Celery schedules it every 30 seconds (`app/workers/celery_app.py:295-300`). There is no hourly request counter in this code.
- A malformed event can raise out of the event loop because event handling has no per-event error guard (`app/workers/tasks/firehose_tasks.py:116-209`). The Celery task sets `max_retries=0` (`:43-50`). A missing or empty login token is clearly warned about and returns `no_token` (`:61-67`); `_get_token` trims the single token or pool entries (`app/infrastructure/scanners/providers/github.py:261-284`). The token pattern and check come from `app/domain/tokens.py:10-50`.
- Results are sent to the validation queue by `_save_credentials_async` (`app/workers/tasks/scanners/common.py:138-177`). Firehose only sends a system log after tokens are enqueued (`app/workers/tasks/firehose_tasks.py:213-215`; helper `app/workers/tasks/scanners/common.py:191-195`), so commit failures do not reach that log. The shared Redis client comes from `app/workers/tasks/flows/common.py:45`. No code changed in S2.1. S2.1 checkpoint commit: `6fde6bf`.

## 2026-10-14 — S2.2 GitHub firehose hardening

- Updated only `app/workers/tasks/firehose_tasks.py`. GitHub status errors, request failures, malformed events, commit replies, and Redis state failures now get warning or error logs that say what will happen next. One bad event or commit fetch is caught so the poll can carry on (`:191-383,457-575`).
- Commit marks are held until result saving succeeds (`:577-608`). A failed fetch gets no mark, and the event marker and ETag are saved only after the whole poll succeeds (`:610-628`). This keeps failed work available for the next poll.
- Added a shared limit of 4,500 requests in one hour, with a shared wait when it fills (`:42-68,92-147,221-234,459-475`). A GitHub rate-limit reply uses its wait headers or a one-minute fallback (`:160-188`). Pages stay capped at three, the commit gap stays 0.2 seconds, and the schedule stays every 30 seconds. Missing login now says the next poll will try again (`:406-410`).
- No tests were run in S2.2; they are the next box. S2.2 checkpoint commit: `1da2c9c`.

## 2026-10-14 — S2.3 GitHub firehose tests

- Added `tests/unit/test_firehose_tasks.py`. The tests call the real firehose poll, event, commit, and result-save code. Only GitHub replies, Redis, the validation queue, and the system-log send are replaced. They cover provider down, events and commit HTTP errors, event and commit rate limits, the hourly cap, empty events, one commit failing while another finishes, retrying the failed commit, missing login, and retry after a result-queue failure.
- The first run had 9 passed and 1 failed: it found that the event helper could not see the token checks imported inside the poll. Passed those real imports into the helper and fixed the failure. Final run: `.venv/bin/pytest tests/unit/test_firehose_tasks.py` — 10 passed, 0 failed. Pytest also printed cache and Starlette warnings; no test failed because of them.
- S2.3 checkpoint commit: `26f2db7`.

## 2026-10-14 — S2.4 GitHub firehose complete

- Committed the firehose task, its failure-path tests, and the final S2.4 state and journal notes to `main`, then pushed to `origin main`. The final focused run passed 10 tests with 0 failures. The 30-second schedule was left unchanged. S2.4 code commit: `b1e9a61`. Stop here for the supervisor to start scanner 3.

## 2026-10-14 — S3.1 Shodan vet

- Read both Shodan files and the shared helpers they use. A missing key returns an empty list with no warning (`app/infrastructure/scanners/providers/shodan.py:5-12`). The request checks HTTP status and reads only `matches` from a 200 reply (`:23-31`), so an error inside a 200 reply looks like no results. HTTP 401/403/404/422 errors fail at once; 429 and 500/502/503/504 are retried up to three times, using `Retry-After` for 429 when it is a whole number (`app/utils/http_client.py:37-72`). Network errors are also retried. When those retries run out, the service returns an empty list (`shodan.py:29-31,118-126`). The service logs most caught errors without the search text; retry warnings also lack it. The worker logs each search before calling, but treats an empty list as success (`app/workers/tasks/scanners/providers/shodan.py:35-55`).
- The main scan catches a thrown error per search and carries on (`app/workers/tasks/scanners/providers/shodan.py:35-49`); the C2 scan does the same (`:115-128`). A 1-second gap follows main searches and a 2-second gap follows C2 searches (`:45,:124`), but there is no shared request pace, page loop, credit check, or wait for plan-limit errors. The service asks only for the default result page and keeps at most 300 matches (`app/infrastructure/scanners/providers/shodan.py:21,48`). Default searches repeat on each scheduled run; the main scan is scheduled every configured 4 hours and C2 every 6 hours (`app/workers/celery_app.py:482-484,576-578`), so searches are tried again, though no failed-search record is kept. No Shodan code changed in S3.1. Checkpoint commit hash is recorded in the next Shodan journal entry.

## 2026-10-14 — S3.2 Shodan hardening

- In `app/infrastructure/scanners/providers/shodan.py`, Shodan errors now raise a clear search error instead of looking like no results. Network and server errors wait and retry; 429 waits for Shodan's `Retry-After` value (or one minute if absent) and retries once, then stops the run if still limited. Credit or plan errors stop the run. Each request asks for page 1 and uses no more than 100 matches, so it does not request extra pages or spend credits for later pages.
- In `app/workers/tasks/scanners/providers/shodan.py`, both scans warn when the key is blank, log each failed search and what happens next, and keep going after ordinary search errors. The main scan saves failed searches with their country in Redis until search and save both succeed; C2 repeats its fixed query list on the next scheduled run. Empty results are logged as a normal result and the scheduled search runs again. The 1-second and 2-second gaps remain in place. System-log send errors no longer stop scanning.
- Added an optional error callback to `app/infrastructure/scanners/common.py`; Shodan uses it to show active-check failures as warnings with the search and host. Other callers keep their earlier behavior. `.venv/bin/python -m py_compile` passed for the three changed files. No tests were run in this step; they are S3.3. S3.1 checkpoint commit: `e739246`. S3.2 checkpoint commit hash will be recorded in the next Shodan journal entry.

## 2026-10-14 — S3.3 Shodan tests

- Added `tests/unit/test_shodan_scanner_failures.py`. These tests call the real Shodan provider and worker. Only the Shodan HTTP replies, Redis, waits, result saving, and system-log sending are replaced. They cover provider outage and retry on the next scan, HTTP 401/403/404/422/500, an error inside a 200 reply, 429 waits and stops, exhausted query credits, no results, blank keys, the 100-result page cap, active-check failures, and one search failing while later searches run in both main and C2 scans.
- The first run had 11 passed and 1 failed: the C2 test expected Redis state, but C2 repeats its fixed query list. I corrected the test to run C2 a second time and check that the same failed query is sent again. Final results: the new file passed 14 tests; Shodan query-preservation passed 2; Shodan/C2 query-coverage passed 13. Total: 29 passed, 0 failed, with 30 unrelated tests deselected. Pytest printed Starlette, Hypothesis, and `datetime.utcnow()` warnings; none caused a test failure. S3.1 checkpoint: `e739246`; S3.2 checkpoint: `f22d5b2`. S3.3 checkpoint hash will be recorded in the next Shodan journal entry.

## 2026-10-14 — S3.4 Shodan code pushed

- Committed the Shodan provider and worker changes, the Shodan-only active-check warning option, the 14 new Shodan failure tests, and the current state and journal notes. The 4-hour main scan and 6-hour C2 schedule were left as they were. The code commit hash will be recorded with the final S3.4 tick. S3.1 checkpoint: `e739246`; S3.2 checkpoint: `f22d5b2`; S3.3 checkpoint: `e05506a`.

## 2026-10-14 — S3.4 Shodan complete

- Pushed the Shodan code, tests, and state notes to `main` as `506defc`. The final focused tests passed 29 with 0 failures. The S3.4 completion tick is committed with this journal entry. Checkpoints: S3.1 `e739246`; S3.2 `f22d5b2`; S3.3 `e05506a`; S3.4 code `506defc`.

## 2026-10-14 — S4.1 URLScan review

- Read the URLScan worker, provider, and shared helpers. When URLScan cannot be reached, the shared helper tries the request up to four times (`app/utils/http_client.py:37-72`), then returns `None`; the provider turns that into an empty result (`app/infrastructure/scanners/providers/urlscan.py:42-44`). Timeouts and network errors are also caught and returned as empty results (`:153-161`). The worker then treats the search as a normal result (`app/workers/tasks/scanners/providers/urlscan.py:45-50`). Since the provider catches these errors, the circuit breaker sees a successful return and does not count the outage (`app/core/circuit_breaker.py:81-89`).
- HTTP 401/403 replies become the text “Invalid URLScan Key”; 404/422 are raised by `raise_for_status`. The provider logs the caught error and returns an empty result (`app/infrastructure/scanners/providers/urlscan.py:37-40,153-161`). HTTP 500 and similar server errors are retried, then become an empty result if retries run out (`app/utils/http_client.py:59-72`, `app/infrastructure/scanners/providers/urlscan.py:42-44`). A 200 reply with an error field is not checked: missing `results` is read as an empty list (`app/infrastructure/scanners/providers/urlscan.py:46-47`).
- A 429 reply waits for `Retry-After` when it is a whole number, or uses a growing wait; the helper makes up to four attempts. After the last attempt it returns `None`, which is treated as no results. There is no clear final message saying the limit or daily quota stopped the search (`app/utils/http_client.py:37-72`, `app/infrastructure/scanners/providers/urlscan.py:42-47`). A 403 limit reply is labelled as a bad key (`:37-40`).
- Empty search results are logged as zero hits and returned normally (`app/infrastructure/scanners/providers/urlscan.py:46-47,150-151`); the worker logs the result count (`app/workers/tasks/scanners/providers/urlscan.py:45-49`). This code only searches URLScan; it does not submit scans or poll for queued scans. It does not check a result's finished state. A DOM lookup only reads a 200 reply; other statuses are ignored, and request errors are written at debug level (`app/infrastructure/scanners/providers/urlscan.py:84-97`). Deep-scan errors also go to debug by default (`app/infrastructure/scanners/common.py:130-149`).
- The worker logs each query, but provider errors do not include that query, and the retry helper logs no URLScan search details. DOM errors and deep-scan errors can be hidden at debug level. Most provider errors look like a successful search with zero results because the provider catches them (`app/infrastructure/scanners/providers/urlscan.py:32,153-161`; `app/workers/tasks/scanners/providers/urlscan.py:42-54`).
- Each search asks for 100 results and does not request another page (`app/infrastructure/scanners/providers/urlscan.py:31`). It caps processed results at 300, or 50 when the time filter finds none (`:64-69`), but the request itself asks for only 100. The worker waits two seconds after each query (`app/workers/tasks/scanners/providers/urlscan.py:42-50`); DOM lookups can still run 20 at once (`app/infrastructure/scanners/providers/urlscan.py:73-79`). The scheduled scan remains every four hours (`app/workers/celery_app.py:486-489`). There is no URLScan scan-submission or polling pace to check because neither action is made here.
- The six built-in queries run again on the next scheduled scan. A one-off query passed to the task is not saved, and failed DOM lookups have no saved retry list; they are tried again only if URLScan returns that scan in a later search (`app/workers/tasks/scanners/providers/urlscan.py:11-20,42-54`; `app/infrastructure/scanners/providers/urlscan.py:84-97`).
- An error that escapes the provider is caught around one query, so later queries still run (`app/workers/tasks/scanners/providers/urlscan.py:42-54`). Most provider errors do not escape because they become empty results (`app/infrastructure/scanners/providers/urlscan.py:153-161`). A missing or empty key logs a warning and returns empty results, with no clear “try next scan” message (`:10-17`); the key is optional in settings (`app/core/config.py:151`). No code changed in S4.1. The S4.1 checkpoint hash will be recorded in the S4.2 entry.

## 2026-10-14 — S4.2 URLScan hardening

- Changed only the URLScan worker and provider. Search replies now report the search and error for HTTP errors, network failure, invalid replies, and errors inside HTTP 200 replies (`app/infrastructure/scanners/providers/urlscan.py:58-146`). Network and server errors wait and retry; rate limits use the provider wait value and stop the run cleanly when the wait is too long or the limit remains (`:58-117`). The worker saves failed searches in URLScan-only Redis retry storage and continues with other searches (`app/workers/tasks/scanners/providers/urlscan.py:8-105,288-353`).
- Search requests still ask for at most 100 results and do not fetch more pages (`app/infrastructure/scanners/providers/urlscan.py:31-33,68`). URLScan DOM requests now run one at a time with a one-second wait; 404, other HTTP errors, and network errors are logged with the search and scan ID (`:220-288`). A rate limit pauses more DOM lookups for that run. Queued or unfinished scans and failed lookups stay saved by scan ID and are checked again later (`:173-213,295-408`; worker retry storage at `app/workers/tasks/scanners/providers/urlscan.py:8-10,108-146,255-286`). The worker keeps the two-second gap between searches, and a rate limit saves the remaining searches (`:288-357`).
- A missing or empty key now gives a clear warning and returns before sending requests (`app/workers/tasks/scanners/providers/urlscan.py:200-211`). Status-log send errors are logged without stopping the scan (`:149-157`). The four-hour schedule and shared scanner helpers were left unchanged. `.venv/bin/python -m py_compile` and `git diff --check` passed; no tests were run in this box. S4.1 checkpoint: `9d8ba99`. The S4.2 checkpoint hash will be recorded in S4.3.

## 2026-10-14 — S4.3 URLScan tests

- Added `tests/unit/test_urlscan_scanner_failures.py`. The tests call the real URLScan worker and provider. They fake only URLScan HTTP replies, Redis, result saving, status messages, and waits. They cover provider outage and retry on the next run; HTTP 401, 403, 404, 422, and 500; an error in HTTP 200; rate limit waits and stops; empty results; a queued scan kept and checked again until its DOM is ready; one failed search while the other built-in searches run; and a missing key. The existing URLScan request-size test was run too.
- The first run had 10 passed and 3 failed. The failures were in the test setup: the recovery call reused the first fake HTTP handler, and two checks expected wording the task does not return. I fixed the test setup and checks. Final command: `.venv/bin/pytest -p no:cacheprovider tests/unit/test_urlscan_scanner_failures.py tests/unit/test_scanner_provider_requests.py::test_urlscan_request_uses_supported_query_and_page_size` — 13 passed, 0 failed. Pytest reported one existing Starlette deprecation warning.
- S4.1 checkpoint: `9d8ba99`; S4.2 checkpoint: `1cacd66`. No other scanner tests or code were changed. The S4.3 checkpoint hash will be recorded in S4.4.

## 2026-10-14 — S4.4 URLScan complete

- Committed the URLScan provider, worker, failure tests, and final state and journal notes to `main`, then pushed them to `origin main` as `8410ef6`. The final focused run passed 13 tests with 0 failures. The four-hour scan schedule and all other scanner code were left unchanged. Checkpoints: S4.1 `9d8ba99`; S4.2 `1cacd66`; S4.3 `0fb9f1d`; S4.4 code `8410ef6`.

## 2026-10-14 — S5.1 GrepApp review

- Read the GrepApp worker, provider, and the shared runner. Network and HTTP errors are caught by the provider and returned as empty results (`app/infrastructure/scanners/providers/grepapp.py:22-30,50-52`), so the runner handles them like a successful empty search (`app/workers/tasks/_scanner/base.py:76-87`). A 200 reply with an error field also looks empty (`app/infrastructure/scanners/providers/grepapp.py:30-34`). A 429 only logs a short warning and stops pages without waiting or trying again (`:26-28`). Empty hits stop pages normally (`:32-34`). The service searches at most five pages, waits one second between pages, and leaves the page result count to the API default (`:22-25,47`). The fixed search runs again on the next task run because no progress is saved (`app/workers/tasks/scanners/providers/grepapp.py:11-19`). There is no login key in this client; it sends a User-Agent header only (`app/infrastructure/scanners/providers/grepapp.py:8-9,22`). The one search cannot block other searches because there are no separate searches in this task. No code changed in S5.1. The S5.1 checkpoint hash will be recorded in S5.2.

## 2026-10-14 — S5.2 GrepApp hardening

- Changed only `app/infrastructure/scanners/providers/grepapp.py`. Network and server errors now wait and retry up to three times; 429 replies wait using `Retry-After` or a rising pause, then stop cleanly if they persist (`:40-93`). The error log names the fixed search and page, says what failed, and says it will run again on the next scan (`:153-159`). Bad JSON replies and error fields inside HTTP 200 replies now fail clearly instead of looking like no hits (`:108-120`). The search checks no more than five pages, one second apart, and stops after 100 hits (`:11-16,105-149`). The shared runner catches a failed one-call search and ends the scan cleanly (`app/workers/tasks/_scanner/base.py:76-87`); this fixed search is called again on the next task run (`app/workers/tasks/scanners/providers/grepapp.py:11-19`). No key is needed. Syntax check and `git diff --check` passed; tests are for S5.3. S5.1 checkpoint: `0dd4bb0`. The S5.2 checkpoint hash will be recorded in S5.3.

## 2026-10-14 — S5.3 GrepApp tests

- Added `tests/unit/test_grepapp_scanner_failures.py`. The tests call the real GrepApp provider and worker, replacing only HTTP replies, Redis, saving, status messages, and waits. They cover provider outage and a later scan, HTTP 401/403/404/422/500, an error inside HTTP 200, rate limiting and long wait requests, empty results, a page request that fails then recovers so later pages run, the five-page limit, one-second spacing, and the 100-hit limit. GrepApp has one fixed search, so there are no separate searches to test independently. Command: `.venv/bin/pytest -p no:cacheprovider tests/unit/test_grepapp_scanner_failures.py` — 13 passed, 0 failed. Pytest showed one existing Starlette warning. Checkpoints: S5.1 `0dd4bb0`; S5.2 `9047c45`. The S5.3 checkpoint hash will be recorded in S5.4.

## 2026-10-14 — S5.4 GrepApp code commit

- The GrepApp provider now checks replies, reports failures with the search and next step, waits before retries, and limits pages and results. Added real-worker tests for outage, HTTP errors, rate limits, empty results, later-scan recovery, page and result limits, and continued paging after a temporary error. The focused run passed 13 tests with 0 failures. Pushed the code, tests, and state and journal notes as `fc0128e`. Checkpoints: S5.1 `0dd4bb0`; S5.2 `9047c45`; S5.3 `6cd722b`; S5.4 `fc0128e`.

## 2026-10-14 — S6.1 Gist review

- Read the Gist task, provider, shared runner, and HTTP helper. The public-gist request asks for 100 items on one page (`app/infrastructure/scanners/providers/github_gist.py:14-31`). If GitHub cannot be reached or returns HTTP 401, 403, 404, 422, or 500, `raise_for_status()` throws; the outer handler logs a general error and returns an empty list (`:28-31,71-73`). The shared runner then treats it like a completed empty search (`app/workers/tasks/_scanner/base.py:76-87`). The task's automatic retries do not help because the provider catches the error and returns (`app/workers/tasks/scanners/providers/github_gist.py:5-7,71-73`).
- The provider does not check for an error field in a 200 reply (`github_gist.py:31`). If GitHub sends an error object instead of a list, its keys are handled as gist entries; those jobs fail on `.get()`, but their exceptions are returned by `gather` and skipped because they are not lists (`:38-44,61-68`). For raw-file fetches, HTTP status is not checked, so an error page is scanned as file text (`:46-50`); network errors are only logged at debug level (`:46-58`). One bad gist does not stop the other gist jobs, but the failed file is not saved for another try.
- A 429 or rate-limit 403 on the public-gist request becomes the same general caught error; there is no wait based on `Retry-After` or GitHub's rate-limit headers (`:28-31,71-73`). Raw-file 429/403 replies are not detected (`:46-50`). Gist reads `GITHUB_TOKENS` but always uses its first entry (`:7-11`). GitHub Search rotates the pool through Redis and uses a shared 6.1-second code-search timer and pause (`app/infrastructure/scanners/providers/github.py:13-15,72-111,261-284`); Gist does not use those parts. When both scanners use the same token, Gist calls spend that token's provider allowance outside GitHub Search's local pacing.
- No gists and gists with no matching tokens both produce an empty list (`github_gist.py:61-69`). The runner logs zero matches but does not say which case happened (`app/workers/tasks/_scanner/base.py:76-87`). The request asks for at most 100 gists and does not fetch another page; raw-file requests are limited to 10 active gist jobs, but there is no wait between requests or rate-limit handling (`github_gist.py:26,34-36,61-68`).
- A failed public-gist request is tried again only when the scheduled task runs again; no failed search or file is saved. A raw-file failure may be tried again only while its gist remains among the newest 100; after that, the missed file is lost. A missing token produces a warning and a clean return (`github_gist.py:17-19`), after which the runner reports an ordinary zero-match result. No scanner code changed in S6.1. The S6.1 checkpoint hash will be recorded in S6.2.

## 2026-10-14 — S6.2 Gist hardening

- Changed only the Gist provider and task. Gist now uses the shared GitHub token rotation counter (`app/infrastructure/scanners/providers/github_gist.py:55-71`) and reads and sets the same rate-limit pause used by GitHub Search (`:11-22,126-164`). The public list asks for 100 results on page 1 and checks HTTP status, JSON, and error messages inside HTTP 200 replies (`:166-225`). Rate limits use GitHub's wait headers, wait and retry once when the wait is 5 minutes or less, and stop cleanly with a clear error for longer or repeated limits (`:185-204`).
- Raw files are requested one at a time with a one-second gap. HTTP errors and network errors are recorded against the gist and file; one failed file does not prevent later files from being checked (`:326-406`). A rate limit pauses later file requests and keeps them for the next scan (`:341-397`). Missing or blank login now gives a clear warning and returns cleanly while keeping saved retries (`:245-257`; worker message at `app/workers/tasks/scanners/providers/github_gist.py:106-120`).
- The worker saves unfinished file URLs in its own Redis list (`github_gist.py:8,38-65`). It writes the files to retry before saving results, removes finished files only after result saving succeeds, and leaves the saved list in place if a save fails (`:137-182`). Redis read or write failures are logged and ask Celery to retry this task (`:83-104,143-153,163-193`). The scan schedule was not changed. Syntax checks and `git diff --check` passed; failure tests are for S6.3. S6.1 checkpoint: `d709a94`. The S6.2 checkpoint hash will be recorded in S6.3.

## 2026-10-14 — S6.3 Gist tests

- Added `tests/unit/test_github_gist_scanner_failures.py`. The tests call the real Gist provider and worker; only GitHub replies, Redis, result saving, scan logs, and waits are replaced (`:109-410`). They cover provider down, HTTP 401/403/404/422/500, an error inside HTTP 200, a secondary rate limit and a long rate wait, empty results, a failed file while another succeeds, retry on the next scan, retry after result saving fails, raw-file rate limits, a missing token, page size, request spacing, and the shared GitHub token rotation.
- Command: `.venv/bin/pytest -p no:cacheprovider tests/unit/test_github_gist_scanner_failures.py` — 15 passed, 0 failed. Pytest printed a Starlette warning and a Hypothesis in-memory warning; neither failed a test. `git diff --check` passed. S6.1 checkpoint: `d709a94`; S6.2 checkpoint: `712144d`. The S6.3 checkpoint hash will be recorded in S6.4.

## 2026-10-14 — S6.4 Gist complete

- Pushed the Gist provider, task, failure tests, and final state and journal notes to `main` as `12f54d3`. The Gist list stays at 100 items on page 1; failed file work stays in Redis until its results save; rate waits use the shared GitHub Search pause; and a failed file does not stop later files. The focused run passed 15 tests with 0 failures. Checkpoints: S6.1 `d709a94`; S6.2 `712144d`; S6.3 `586980c`; S6.4 code `12f54d3`.

## 2026-10-14 — S7.1 Exa review

- Confirmed S1.1–S6.4 were ticked and the tree had only the existing untracked `.agents/handoffs/` folder. Read the Exa worker, client, shared helpers used by Exa, key tests, and its schedule. The client asks for 25 results, one page, six paste domains, and up to 10,000 text characters (`app/infrastructure/scanners/providers/exa.py:50-58`); the worker waits two seconds between searches (`app/workers/tasks/scanners/providers/exa.py:38-54`). The shared helper makes up to four requests for network errors, 429, and 500/502/503/504, waiting with `Retry-After` or rising 2/4/8 second waits; it returns `None` after those tries, while other 4xx replies fail at once (`app/utils/http_client.py:37-72`).
- If Exa is down, has an HTTP error, or returns invalid JSON, the client logs a general error and returns `[]` (`app/infrastructure/scanners/providers/exa.py:60-73,93-95`). The log omits the search text. A valid HTTP 200 error object without `results` also becomes `[]` (`:68-73`). A real empty result is likewise `[]`, so the worker reports it as a successful query with zero matches (`app/workers/tasks/scanners/providers/exa.py:38-43`). Rate-limit waits are logged only by the shared HTTP helper without the search; when retries run out, the client treats the `None` reply as empty. The worker creates an unused error list (`:35-36,44-47`).
- Missing or blank keys are filtered out; with none, the client warns without naming the search and returns `[]` (`app/infrastructure/scanners/providers/exa.py:26-43`). Each request picks a random configured key, but a rejected key is not removed and the request is not retried with another key (`:26-37,60-71`). The task's 401/403 cooldown and stop path is behind its exception handler (`app/workers/tasks/scanners/providers/exa.py:44-53`), but the client catches provider errors first, so an Exa auth reply normally does not reach it. The task's automatic retry applies only to raised exceptions (`:5-12`); provider errors are swallowed, so no failed search is held for retry. The three default searches are rebuilt on each run and the schedule calls the task every 12 hours (`:26-30`; `app/workers/celery_app.py:507-509`), so they may run again then, but one-off query text is not saved. The worker carries on after a client failure returns `[]`; its outer handler also carries on except when an exception text looks like 401, 403, or auth (`app/workers/tasks/scanners/providers/exa.py:38-54`). No scanner code changed in S7.1. The S7.1 checkpoint hash will be recorded in S7.2.

## 2026-10-14 — S7.2 Exa hardening

- Changed only the Exa provider and Exa task. The provider now checks HTTP errors, JSON replies, and error fields in HTTP 200 replies; reports the search and next action; tries other configured keys after 401/403; and treats blank keys, quota replies, and rate limits as clear failures. It asks for 25 results, keeps the one-page request and 10,000-character text cap, and waits between searches. Exa's current docs say Search allows up to 100 results and does not support pages, so 25 stays within its result limit.
- The worker keeps unfinished searches in a Redis list, loads them on the next run, and clears a saved search only after the search and result enqueue finish. A failed search is kept while later searches continue. Missing or rejected keys, exhausted quota, and a continuing rate limit keep the current and later searches, then end the run cleanly. Network and server errors get up to four attempts with rising waits; 429 replies use Exa's `Retry-After` value or a rising wait, and the run stops if Exa asks for more than 60 seconds or keeps limiting requests. Existing key selection remains random, with other configured keys tried after a rejection. The scan schedule is unchanged.
- `git diff --check` passed. Tests are for S7.3. S7.1 checkpoint: `0ffce36`. The S7.2 checkpoint hash will be recorded in S7.3.

## 2026-10-14 — S7.3 Exa tests

- Added `tests/unit/test_exa_scanner_failures.py`. The tests call the real Exa provider and task; only HTTP replies, Redis, result saving, status output, and waits are replaced. They cover provider outage and next-run retry, HTTP 404/422/500, an error in HTTP 200, rate limiting, quota exhaustion, empty results, a failed search while later searches continue, missing or blank keys, and trying the next key after 401/403. They also check the 25-result request, text limit, no page request, and two-second spacing between searches.
- Ran `.venv/bin/pytest -p no:cacheprovider tests/unit/test_exa_scanner_failures.py tests/unit/test_exa_key_rotation.py` — 13 passed, 0 failed. Pytest showed a Starlette deprecation warning and a Hypothesis in-memory warning; neither failed a test. S7.1 checkpoint: `0ffce36`; S7.2 checkpoint: `8f77193`. The S7.3 checkpoint hash will be recorded in S7.4.

## 2026-10-14 — S7.4 Exa completion

- Finished the Exa provider and worker changes and added real-code failure tests. The provider reports the search and error, waits and retries within set limits, checks 200 replies for errors, and handles missing keys, rejected keys, quota replies, and rate limits. The worker keeps failed searches in Redis until their search and result enqueue finish, continues after ordinary search errors, and stops cleanly on missing keys or provider limits while keeping all unfinished searches. The Exa schedule remains every 12 hours.
- Final check changed successful replies to parse once, then reran `.venv/bin/pytest -p no:cacheprovider tests/unit/test_exa_scanner_failures.py tests/unit/test_exa_key_rotation.py` — 13 passed, 0 failed. Pytest showed a Starlette deprecation warning and a Hypothesis in-memory warning; neither failed a test. `git diff --check` passed. Exa's [Search API guide](https://exa.ai/docs/search/quickstart) says Search allows up to 100 results and does not support pages; this scan asks for 25.
- Checkpoints: S7.1 `0ffce36`; S7.2 `8f77193`; S7.3 `609a6c9`; S7.4 code `b5e5b4a`.

## 2026-10-14 — S8.1 Rentry review

- Read the Rentry task, Rentry provider, worker shared helpers, infrastructure shared helpers, the shared scan runner, Exa search code called by Rentry, and the Rentry schedule. S1.1–S7.4 were ticked; the only pre-existing working tree item was `.agents/handoffs/`.
- Rentry uses two fixed Exa searches (`app/infrastructure/scanners/providers/rentry.py:19-28`). Exa asks for 25 results and one response list (`app/infrastructure/scanners/providers/exa.py:9-12,112-139`); it retries network/server errors and 429 up to four requests, waiting by `Retry-After` or rising delays (`:153-192`). Exa logs and returns an empty list to Rentry after a final failure because Rentry does not request errors to be raised (`rentry.py:27-28`; `exa.py:222-227,253-281`). The fixed searches run again on later scans, but there is no saved retry list.
- Raw paste HTTP errors are silently skipped before the one-second wait (`rentry.py:55-59,73`), and fetch exceptions are only logged at debug level (`:70-71`). A 200 reply is scanned without checking for error or loading text, then marked seen for seven days even when it contains no token (`:60-69`). Failed HTTP and transport fetches are not marked seen, so they are retried only if Exa returns that URL on another scan; there is no saved paste retry list. A failed search or paste does not stop the other fixed query or later paste matches (`:25-30,55-76`).
- Exa rate limits have bounded waits, but Rentry raw 429 replies are treated like other HTTP errors and skip the one-second delay (`rentry.py:55-59,73`). Rentry has no login key; its Exa dependency needs a key. Exa warns when no non-empty key is set, then gives Rentry an empty list (`exa.py:194-198,253-267`). Rentry remains scheduled every 12 hours (`app/workers/celery_app.py:511-514`). No scanner code changed in S8.1. The S8.1 checkpoint hash will be recorded in S8.2.

## 2026-10-14 — S8.2 Rentry hardening

- Changed only the Rentry provider and task. Searches now ask Exa to raise failures; Rentry logs each search and keeps checking the other fixed search. Missing Exa keys are reported by Exa and then clearly logged by Rentry. Both fixed searches still run on the next scheduled scan.
- Rentry now saves each found paste ID in Redis before fetching it. Failed fetches, missing or deleted pastes, empty or still-loading replies, error replies, and save failures stay in that set for the next scan. A paste is marked complete only after a clean read with no token, or after its found results save. Each failure log names the search or paste, the problem, and what happens next.
- Raw fetches now wait between pastes. Network and server failures get up to four tries with rising waits. HTTP 429 replies use `Retry-After` or a rising wait; after a wait above 60 seconds or four continuing rate replies, raw fetches stop cleanly and all remaining pastes stay saved. Exa still asks for 25 results per search response and has no page loop; the Rentry schedule remains every 12 hours.
- `git diff --check` and Python syntax compilation passed. Tests are for S8.3. S8.1 checkpoint: `eb4fd58`; the S8.2 checkpoint hash will be recorded in S8.3.

## 2026-10-14 — S8.3 Rentry tests

- Added `tests/unit/test_rentry_scanner_failures.py`. The tests call the real Rentry provider, Exa client, task, and shared scan runner. Only HTTP replies, Redis, result saving, status output, and waits are replaced.
- Covered Exa outage and missing key; Rentry connection failure; HTTP 401, 403, 404, 410, 422, and 500; error and loading replies with HTTP 200; rate limit waits and a clean stop; empty search results; a failed paste while a later paste succeeds; and a result save failure that keeps the paste for another scan.
- Ran `.venv/bin/pytest -p no:cacheprovider tests/unit/test_rentry_scanner_failures.py` — 15 passed, 0 failed. The first run had one case-sensitive log assertion failure; the assertion was corrected and the full file was rerun successfully. Pytest reported one Starlette deprecation warning and two Hypothesis in-memory setup warnings; none failed a test. S8.1 checkpoint: `eb4fd58`; S8.2 checkpoint: `9dad0cb`; the S8.3 checkpoint hash will be recorded in S8.4.

## 2026-10-14 — S8.4 Rentry final commit

- Final Rentry changes are limited to the Rentry provider, its task, and `tests/unit/test_rentry_scanner_failures.py`. The scanner now logs failures, keeps unfinished paste IDs in Redis for the next scan, retries network and server errors with waits, and slows or stops raw fetches after rate limits. The 12-hour schedule is unchanged.
- Ran `.venv/bin/pytest -p no:cacheprovider tests/unit/test_rentry_scanner_failures.py` — 15 passed, 0 failed. `git diff --check` and Python syntax compilation passed. S8.1 checkpoint: `eb4fd58`; S8.2 checkpoint: `9dad0cb`; S8.3 checkpoint: `9640ee8`; S8.4 code commit: `83f4c04`.

## 2026-10-14 — S9.1 Hastebin review

- Read the Hastebin task and provider, the shared scanner helpers, the scan runner, and the Exa code Hastebin calls. S1.1–S8.4 were checked; the tree was clean apart from `.agents/handoffs/`.
- Hastebin runs two fixed Exa searches (`app/infrastructure/scanners/providers/hastebin.py:19-22`). It calls Exa without `raise_on_error` (`:27`), then treats an empty reply as no results (`:28-29`). Exa logs network/server and rate-limit failures, waits and retries bounded requests, then returns an empty list by default (`exa.py:153-192,222-281`). The fixed searches run again on later scheduled scans, but failed searches are not saved for a retry.
- Raw paste HTTP errors, including 401/403/404/422/429/500, are skipped with no Hastebin log (`hastebin.py:56-60`). A raw fetch exception is only logged at debug level (`:71-72`). HTTP 429 has no wait or retry, and the one-second pause at `:74` is skipped on the non-200 path. A failed fetch is not marked seen, so it can be tried again only if Exa returns it later; Hastebin has no saved retry list.
- Every HTTP 200 body is scanned as text and then marked seen for seven days (`hastebin.py:61-70`), without checking for an error, missing-paste, deleted-paste, or loading reply. An empty Exa reply and a paste with no token are otherwise quiet. A fetch exception or HTTP error does not stop later pastes; an Exa error is caught and the next fixed search runs (`:25-30,71-77`). The shared runner catches an uncaught single-search error and returns a failed scan result (`app/workers/tasks/_scanner/base.py:76-87`).
- The scan has no Hastebin login key. Its Exa dependency needs at least one non-empty `EXA_API_KEY` entry and warns/errors then returns no results if none is set (`exa.py:71-85,194-198,253-267`). Exa asks for at most 25 results with no page loop (`exa.py:9-12,112-139`); Hastebin waits one second after ordinary raw fetch handling, but not after a non-200 reply (`hastebin.py:56-74`). Hastebin runs every 12 hours (`app/workers/celery_app.py:515-517`). No scanner code changed in S9.1. S9.1 checkpoint: `44286cd`.

## 2026-10-14 — S9.2 Hastebin hardening

- Changed only the Hastebin provider and task. Hastebin now asks Exa to raise search errors, logs the failed query and next step, continues to the other query after an ordinary failure, and stops more Exa searches for a missing key, empty quota, or rate limit.
- Raw paste fetches now retry connection and server errors up to three times with waits. HTTP 429 uses `Retry-After` or a short rising wait; a wait over 60 seconds or three continuing rate replies stops raw fetches for this run. A rate-limit message inside HTTP 200 also stops raw fetches. The one-second pause remains between paste fetches. Exa still asks for up to 25 results per query with no page loop, and the two Hastebin searches and 12-hour schedule are unchanged.
- Failed, missing, deleted, empty, error, and loading replies are logged with the paste ID and remain in a Hastebin-only Redis pending set for the next scan. A clean paste without a token is marked read; a paste with tokens is marked read only after the save step succeeds. One paste failure does not stop later pastes, except that a rate limit stops the remaining raw fetches cleanly.
- `.venv/bin/python -m py_compile` for the two Hastebin files and `git diff --check` passed. S9.1 checkpoint: `44286cd`; S9.2 checkpoint: `963db9d`.

## 2026-10-14 — S9.3 Hastebin tests

- Added `tests/unit/test_hastebin_scanner_failures.py`. It calls the real Hastebin task, provider, shared scan runner, and Exa service. Only outside replies, Redis, saving, status logs, and waits are replaced.
- Covered Exa outage and search continuation; HTTP 401, 403, 404, 410, 422, and 500; empty, loading, missing, and error replies with HTTP 200; raw service outage while a later paste succeeds; rate-limit wait and clean stop; empty search results; missing Exa key; and save failure with the paste left pending.
- First run: 16 passed, 1 failed because the rate-limit test expected insertion order while the scanner reads the sorted pending list. The test IDs were changed so the limited paste sorts first. After that fix, 17 passed. Added checks for a rate-limit message inside HTTP 200 and a malformed search URL; final run: `.venv/bin/pytest -p no:cacheprovider tests/unit/test_hastebin_scanner_failures.py` — 19 passed, 0 failed. One Starlette deprecation warning and two Hypothesis memory warnings did not fail tests. Syntax compilation and `git diff --check` passed.
- S9.1 checkpoint: `44286cd`; S9.2 checkpoint: `963db9d`; S9.3 checkpoint: `0ad75ff`.

## 2026-10-14 — S9.4 Hastebin final commit

- Committed the Hastebin provider, task, and failure tests with the S9.4 tick and notes. The scanner logs search and paste failures, keeps unfinished work for the next scan, backs off on failures, and stops raw requests cleanly after a rate limit. The two fixed searches, Exa's 25-result cap, and Hastebin's 12-hour schedule remain unchanged.
- Final run: `.venv/bin/pytest -p no:cacheprovider tests/unit/test_hastebin_scanner_failures.py` — 19 passed, 0 failed. Syntax compilation and `git diff --check` passed. The code commit hash will be recorded in the next journal note.
- S9.1 checkpoint: `44286cd`; S9.2 checkpoint: `963db9d`; S9.3 checkpoint: `0ad75ff`.
