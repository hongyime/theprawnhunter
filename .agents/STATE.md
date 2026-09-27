# theprawnhunter — STATE

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

## Reviewed workspace maintenance - 2026-09-27

Publish the reviewed portability and privacy maintenance from the current default branch, preserving concurrent upstream work and original workspace changes. Validation is limited to the documented offline fixtures and hosted checks; no live data job or deployment command was executed locally.

2026-09-27: Runtime, development-launcher, and publishing enhancements were withdrawn after safety review. Only privacy and defensive maintenance remain in scope. No image build, runtime deployment, publication, commit, or push was performed.

<!-- MOLT_AUTO_START -->
## Auto State

- Updated: 2026-09-27 22:43:10 +08:00
- Machine: PRAWN-E14
- Harness: claude
- Event: stop
- Branch: maintenance/prawn-ui-20260916
- HEAD: 5094299
- Dirty files: 3
- Resume hint: Read .agents/STATE.md, then the latest file in .agents/handoffs/ if present.
<!-- MOLT_AUTO_END -->
