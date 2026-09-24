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