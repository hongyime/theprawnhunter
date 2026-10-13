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

- Read the GitHub worker, provider, and shared helpers. Network failures and HTTP errors become named search errors (`app/infrastructure/scanners/providers/github.py:222-239,381-397`); rate replies are detected and given a wait (`:182-220`); empty results count as success (`:280-281`). The worker logs failed searches, keeps them in Redis, carries on after ordinary errors, and stops after a rate limit (`app/workers/tasks/scanners/providers/github.py:108-163`). Missing login is clearly reported (`:93-99`; provider `:253-258`). It asks for 100 results and runs no more than 10 searches with at least 6.1 seconds between them (`:51,58-64,83,162-163`; provider `:267`). The weak points are that separate runs share no rate timer, and repo/file fetches can keep sending queued requests after a rate reply (`app/infrastructure/scanners/providers/github.py:116-128,285-288,364-375`). GitHub's current docs set code search to 10 requests per minute and 100 results per page; S1.2 will fix the two weak points. S1.1 checkpoint commit: this entry's commit.

## 2026-10-14 00:11 SGT — S1.2 GitHub search hardening

- Added a shared Redis timer so code searches from separate workers stay at least 6.1 seconds apart. A rate reply now sets a shared wait. Repo and file fetch groups stop sending new requests as soon as one sees a rate limit, with no more than three already-started requests in that group. Failures still stay in the retry set and are only removed after a search and save succeed. The 10-search run cap and scan schedule are unchanged. S1.1 checkpoint: `4fa330f`. S1.2 checkpoint commit: this entry.

