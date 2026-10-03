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
