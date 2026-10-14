# theprawnhunter — STATE

## Current task — Ralph loop: tail work (started 2026-10-14)

- **Job:** Grind through the remaining tail work, one item at a time, one Codex run per item. Full spec: /tmp/ralph-tail-spec.md. SOLE WRITER on this repo.
- **Discipline (Bryan):** After EVERY small task, update STATE.md (tick the checkbox) AND append a JOURNAL.md line (what was done, why, commit hash). Commit STATE.md promptly with each fix commit.
- **Task list:**
  - [x] 1a. Extension tests — small runnable suite proving results kept until upload succeeds.
  - [x] 1b. Scraper batch helper test — real helper, failure warns + stays retryable.
  - [x] 1c. Two older fake-helper tests — now exercise the real bot-reply helper.
  - [x] 2. Notify re-take investigation (@notifyGitlabRepobot) — DONE: live scrape log confirms foreign webhook detected and honeypot setWebhook succeeded; DB records honeypot_active=true.
  - [x] 3. Real Telegram redirect error — DONE: live failures carry `HTTP 403: Forbidden: bot was blocked by the user`.
  - [x] 4. Dependabot pytest alert — updated development pytest pins to patched versions; GitHub marked alert #58 fixed.
  - [ ] F1. Rebuild ONLY affected containers, verify healthy, brief log watch.
- **Next:** Item 4 evidence recorded; this run stops here.

### Item 4 evidence — 2026-10-14

- GitHub Dependabot alert #58 was the sole open moderate alert: `pytest` in `requirements-dev.txt`, GHSA-6w46-j5rx-g56g / CVE-2025-71176, vulnerable range `<9.0.3`, patched version `9.0.3`.
- Updated `requirements-dev.txt` from pytest 8.3.5 / pytest-asyncio 0.24.0 to pytest 9.0.3 / pytest-asyncio 1.4.0, matching the already-pinned compatible versions in `requirements.txt`. Dependency fix commit `889f766` was pushed to `main`.
- After the push, GitHub's dependency graph SBOM showed pytest 9.0.3 and Dependabot alert #58 changed to `fixed` at `2026-10-14T02:08:22Z`.
- Ran `./.venv/bin/python -m pytest tests/unit/` under pytest 9.0.3 + pytest-asyncio 1.4.0: 566 passed, 2 failed (568 total). The failures were `test_telemetry_command_restricts_non_admin` (expected reply text differs from `Admin only.`) and `test_log_calls_never_interpolate_raw_telegram_user_ids` (found three source locations); neither touches the dependency files changed here. No application code was changed.

### Item 3 evidence — 2026-10-14

- The redirect detail fix is commit `5a370ed` (2026-10-13 23:09:43 SGT / 15:09:43 UTC). Queried live `honeypot_updates` rows with non-null `redirect_error` and `received_at` on or after that UTC time: 3 rows, received from 2026-10-13 15:48:20 to 19:04:22 UTC. All 3 contain `HTTP 403: Forbidden: bot was blocked by the user`.
- Queried live worker logs from the fix time onward: 491 `[Redirect] FAILED` entries, spanning 2026-10-14 00:18:26–01:59:51 local. All carry the same HTTP status and Telegram error text. The answer is: **HTTP 403 — Forbidden: bot was blocked by the user.**
- This is a live Telegram response; no failure was fabricated. No code or credentials were changed.

### Item 2 evidence — 2026-10-14

- The live worker's source contains the `scrape_history` takeover preflight and `takeover_only` path from commit `867f25d`; runtime config has honeypot mode, receiver, secret, and AUTO allowlist enabled, and the target credential passes the allowlist check.
- `docker logs --since 48h theprawnhunter_worker`: credential `e34b2ee0-ddcc-4479-bd9f-491a051d1ee5` (`@notifyGitlabRepobot`) began exfiltration at 00:52:40 UTC. At 00:52:51 UTC the worker logged a foreign webhook, then `setWebhook succeeded ... (foreign webhook re-taken over)`, and continued into the scraper flow. At 00:52:52 UTC, the scrape audit recorded `bot_api_updates` with `getUpdates_status=409` and `honeypot_active=true`; the scrape completed as `no_new_messages`.
- The foreign-webhook URL in the raw worker log contains a Telegram Bot API token. The value is redacted in this record. No credential changes or unrelated code changes were made.
- Diagnosis: `.agents/diagnosis/2026-10-14-notifygitlabrepobot-retake.md`.

## Current task (updated 2026-10-14) — remaining 2 findings DONE; worker rebuilt + verified healthy; log watch clean (0 errors, 0 redirect failures)

## Previous current task (updated 2026-10-14)

- **Job:** Fix hidden backfill failures, then check whether overlapping runs can update the same rows.
- **Status:** [x] Part A: the Celery backfill task now retries exceptions, stores its result, and the test confirms a failed database update enters Celery retry and the retry processes the unchanged row. Focused tests: 8 passed, 0 failed.
- **Status:** [x] Part B: stale candidates could be updated again after the broadcaster made a row eligible. Apply runs now use a shared Redis lock; the overlap regression passes. Focused tests after both parts: 9 passed, 0 failed.
- **Next:** None; both fix commits are pushed and verified on `origin/main`.

## Previous task — scanner hardening (completed)

- **Job:** Check and harden all 12 bot-discovery scanners. One checkbox at a time. No jumping ahead, no wandering. SOLE WRITER on this repo — the 3-fix job has stood down; its Fixes 2–3 wait until this job fully completes.
- **Status:** ALL 12 SCANNERS DONE + F1/F2 done. Vet-and-harden job COMPLETE. Next: Fixes 2-3 (3-fix job resumes).
- **Blocked (2026-10-14 ~03:42 SGT):** 3410 cannot resolve hostnames. Tailscale DNS 100.100.100.100 does not answer (Python resolver: Name or service not known). IP connectivity works (ping 8.8.8.8 0% loss; curl to GitHub by direct IP returns 200). Codex cannot reach its backend and pushes cannot reach GitHub until name resolution recovers. Supervisor did NOT change any network/DNS settings (read-only diagnosis only — fixing DNS on the production machine needs Bryan's call). Resume: when name resolution works again, relaunch scanner 11 with /tmp/codex-scanner-11-telegram-prompt.md, then scanner 12, then supervisor does F1 rebuild + F2 log watch.
- **Next-step:** Vet job complete. Supervisor reports; Fixes 2-3 resume next.
- **Discipline (Bryan):** After EVERY small task, update STATE.md (tick the checkbox, set next step) AND append a JOURNAL.md line (what was done, why, commit hash). No step is done until both files are updated. Commit STATE.md promptly with each fix commit so it cannot be clobbered.
- **Task list — every task small enough to finish in one step:**
  - [x] S1.1 Vet GitHub search — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S1.2 Harden GitHub search — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S1.3 Test GitHub search — failure paths covered with REAL code, no fake stand-ins.
  - [x] S1.4 Commit GitHub search — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S2.1 Vet GitHub firehose — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S2.2 Harden GitHub firehose — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S2.3 Test GitHub firehose — failure paths covered with REAL code, no fake stand-ins.
  - [x] S2.4 Commit GitHub firehose — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S3.1 Vet Shodan — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S3.2 Harden Shodan — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S3.3 Test Shodan — failure paths covered with REAL code, no fake stand-ins.
  - [x] S3.4 Commit Shodan — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S4.1 Vet URLScan — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S4.2 Harden URLScan — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S4.3 Test URLScan — failure paths covered with REAL code, no fake stand-ins.
  - [x] S4.4 Commit URLScan — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S5.1 Vet GrepApp — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S5.2 Harden GrepApp — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S5.3 Test GrepApp — failure paths covered with REAL code, no fake stand-ins.
  - [x] S5.4 Commit GrepApp — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S6.1 Vet Gist — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S6.2 Harden Gist — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S6.3 Test Gist — failure paths covered with REAL code, no fake stand-ins.
  - [x] S6.4 Commit Gist — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S7.1 Vet Exa — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S7.2 Harden Exa — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S7.3 Test Exa — failure paths covered with REAL code, no fake stand-ins.
  - [x] S7.4 Commit Exa — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S8.1 Vet Rentry — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S8.2 Harden Rentry — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S8.3 Test Rentry — failure paths covered with REAL code, no fake stand-ins.
  - [x] S8.4 Commit Rentry — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S9.1 Vet Hastebin — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S9.2 Harden Hastebin — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S9.3 Test Hastebin — failure paths covered with REAL code, no fake stand-ins.
  - [x] S9.4 Commit Hastebin — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S10.1 Vet Wayback — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S10.2 Harden Wayback — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S10.3 Test Wayback — failure paths covered with REAL code, no fake stand-ins.
  - [x] S10.4 Commit Wayback — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S11.1 Vet Telegram search — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S11.2 Harden Telegram search — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S11.3 Test Telegram search — failure paths covered with REAL code, no fake stand-ins.
  - [x] S11.4 Commit Telegram search — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S12.1 Vet Netlas — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S12.2 Harden Netlas — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S12.3 Test Netlas — failure paths covered with REAL code, no fake stand-ins.
  - [x] S12.4 Commit Netlas — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] F1 Rebuild ONLY the worker container, verify healthy.
  - [x] F2 Log watch — brief watch, no new crashes.
- **Rules for every scanner:** No port changes, no database changes, no login/secret changes. Do NOT change scan times (the 4h/6h/12h rhythm stays as-is).
- **Supervision rules (Bryan 2026-10-13):** small steps, one at a time, no wandering; update STATE.md + JOURNAL.md after EACH small step; liveness checks every few minutes (process alive, log growing, deliverable moved); on crash, verify state from scratch and resume from STATE.md checkpoint, never restart from zero. Tests must use REAL code, no fake stand-ins.
- **How this job runs:** One Codex run per scanner, one commit per scanner. The supervisor starts each run, watches it, checks the commit landed, then starts the next.

Last updated: 2026-09-18 (prawn-ui batch)

## Repo identity
- Owner: hongyime
- Remote: https://github.com/hongyime/theprawnhunter.git
- Upstream fork: https://github.com/0x6rss/matkap.git
- Branch: maintenance/prawn-ui-20260916 (reused for follow-up chores after PR #21 merged)
- License: Apache-2.0

## Stack
- Backend: Python 3.11 (FastAPI + Celery + Redis + Supabase Postgres) — 10 Docker Compose services
- Frontend: Next.js (frontend/), TypeScript, Vitest, deployed to Vercel (project `theprawnhunter`)
- Extension: Manifest V3 Chrome extension (extension/)
- Test: pytest + pytest-asyncio (Python), vitest (frontend)
- Root package.json is a thin shim (only `puppeteer-core` + FOFA scan scripts). Real workloads live in `frontend/package.json` and Python.

## Deployment
- Vercel project: `theprawnhunter` (projectId: prj_uhn1QEFSTzn33kI2FFXpscgyqt6n, orgId: team_ARK7HKobyCMp0PCArQTLxbz6)
- Docker Compose for the backend stack (docker-compose.yml, docker-compose.prod.yml)
- CI: GitHub Actions — ci.yml, bandit.yml, semgrep.yml, trufflehog.yml, supabase-keep-alive.yml, labeler.yml, greetings.yml, operator-authorization.yml

## Vercel / frontend-linking status ("unlinked frontend source")
- Both `.vercel/project.json` (repo root) AND `frontend/.vercel/project.json` exist locally, each pointing to the SAME Vercel project (prj_uhn1QEFSTzn33kI2FFXpscgyqt6n).
- `.vercel/` is gitignored (see .gitignore line 81: `.vercel`), so neither file is tracked. They are per-checkout artifacts of `vercel link`.
- Frontend build config (`frontend/vercel.json`) is correct: framework=nextjs, buildCommand=`npm run typecheck && npm run build`, outputDirectory=`.next`, region=iad1.
- The remediation-checkout ambiguity: whichever directory `vercel deploy` is invoked from will win. Best practice going forward: run `vercel` only from `frontend/`, and delete the stray root `.vercel/` locally.
- No repo-side fix is required (both are gitignored); this is a per-checkout workflow discipline issue.

## Open issues / PRs
- Open issues: 0
- Open PRs: 0 (PR #21 merged 2026-09-17T23:08:16Z, merge commit a85decc; already in `main`)

## Recent activity
- 2026-09-18: Prawn UI visual style applied to frontend (PR #21: `maintenance/prawn-ui-20260916`)
- 2026-09-12 → 2026-09-14: operator-authorization guard hardening (PR #20 merged: `maintenance/operator-guard-20260912`)
- CI: labeler PR label access fix (e7c30d6), labeler config loading fix (b9475ec)
- Tests: auth control differentiation (3ae15d2)
- Docs: README + PRD rewrite for `03-document`

## Known local branches (unpushed)
- `maintenance/check-bootstrap-20260914` (local only)
- `shell/standardise` (local only)
- `remediation/2026-09-06` (has remote counterpart)

## Free-tier compliance
- Supabase Free (500 MB DB — README notes Pro recommended for sustained use; caller responsibility)
- Vercel Hobby: single project deploy from `frontend/`
- Docker Compose is self-hosted (not counted against Vercel or Supabase quotas)

## Gaps / follow-ups
- No root-level `AGENTS.md` — the task expectation "AGENTS.md exists" is unmet. Deferred: creating one is beyond a "targeted low-risk fix" and requires product-owner decisions on conventions.
- `.agents/` currently only holds `diagnosis/` and `tickets/` subdirectories; STATE.md + JOURNAL.md added in this session.

## Docker Compose minimal-footprint reorg (this session)

- Rewrote `docker-compose.yml`: 4 split celery workers (worker-core/scanners/scrape/validators, 2 CPU/2g each) consolidated into ONE combined `worker` consuming all 4 queues (celery,scanners,scrape,validation) at 1 CPU/768m -- scale horizontally with `--scale worker=N` or `WORKER_CONCURRENCY` instead of separate containers. `bot`, `flower`, `frontend` moved behind Compose `profiles` (`bot`/`monitoring`/`frontend`/`full`) so the DEFAULT `docker compose up -d --build` now starts only redis+api+worker+beat (the minimal dev stack) -- the other three services are opt-in. Added shared `x-logging`/`x-app-build` YAML anchors (DRY). Tightened every service's resource knobs down (redis 0.5cpu/1280m->0.25cpu/320m; api 2cpu/2g->1cpu/512m; worker 2cpu/2g x4 services->1cpu/768m x1; etc.) and shortened healthcheck timeouts to match the smaller footprint. `restart: always` -> `restart: unless-stopped` throughout (won't auto-restart a manually-stopped dev container). Single explicit network (`theprawnhunter_net`) and the 4 legacy-prefixed external volumes are unchanged -- no data migration needed.
- Updated `docker-compose.prod.yml` overlay to match: single `worker` override (concurrency 6 instead of the old per-queue 8/8/6/16 overrides across 4 services).
- Validated with `docker compose config --quiet` (base), `--profile full config --quiet`, and `-f docker-compose.yml -f docker-compose.prod.yml --profile full config --quiet` -- all exit 0. No container was started; no image was built; no `docker compose up` was run at any profile or overlay.
- Docker Desktop state checked before and after: zero theprawnhunter containers/networks exist locally (matches the user's report of having deleted the stack). All 4 required external volumes (`telegramhunter_redis_data`, `_sessions`, `_imports`, `_beat_schedule`) are still present and intact -- `down`/container deletion does not remove externally-named volumes, so there is no data loss to rectify. One unrelated, unnamed, auto-generated container (`ecstatic_thompson`, image `35d01a69f9fd`, running an `apt-get` shell) was observed transiently on this shared machine during this session and exited/was removed on its own before any action was taken on it -- confirmed unrelated to theprawnhunter (no `theprawnhunter_*` name, no theprawnhunter image hash) and left untouched throughout.
- Merged this branch directly into `main` (fast-forward, commit `b11d174`) and pushed both -- `main` and `maintenance/prawn-ui-20260916` are now identical (0/0 divergence). PR-creation via the GitHub API MCP tool failed with an auth-scope error (same limitation observed on other repos this session); given the user's explicit "merge to main if needed" authorization and the fully-validated, docs+declarative-YAML-only diff, merged and pushed directly via git instead of leaving an open PR -- `main` accepted the push with no branch-protection rejection.

## Reviewed workspace maintenance - 2026-09-27

Publish the reviewed portability and privacy maintenance from the current default branch, preserving concurrent upstream work and original workspace changes. Validation is limited to the documented offline fixtures and hosted checks; no live data job or deployment command was executed locally.

2026-09-27: Runtime, development-launcher, and publishing enhancements were withdrawn after safety review. Only privacy and defensive maintenance remain in scope. No image build, runtime deployment, publication, commit, or push was performed.

<!-- MOLT_AUTO_START -->
## Auto State

- Updated: 2026-09-28 22:08:20 +08:00
- Machine: PRAWN-E14
- Harness: claude
- Event: stop
- Branch: main
- HEAD: 2d5becb
- Dirty files: 0
- Resume hint: Read .agents/STATE.md, then the latest file in .agents/handoffs/ if present.
<!-- MOLT_AUTO_END -->

## Checkpoint 2026-10-13 23:12 SGT — redirect error fix (resumed)

- Fix verified on disk; 9/9 tests pass (supervisor-run). Next: commit, push, rebuild worker, 10-min log watch.

## Completed check — login cleanup import

- **Current task:** Verify the reported missing `schedule_deletion` import in `app/services/_bot_listener/login.py`.
- **Progress:** Already fixed on `main`; line 4 imports it from `.commands`, where the helper is defined. Commit `cb8ed1c0f235e0f0545c5119f77e37e5124d5956` adds that import. No code change or tests were needed.
- **Next step:** No follow-up for this check; continue with the existing next task when directed.
