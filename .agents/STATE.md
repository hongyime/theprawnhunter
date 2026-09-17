# theprawnhunter — STATE

Last updated: 2026-09-18 (prawn-ui batch)

## Repo identity
- Owner: hongyime
- Remote: https://github.com/hongyime/theprawnhunter.git
- Upstream fork: https://github.com/0x6rss/matkap.git
- Branch: maintenance/prawn-ui-20260916 (open PR #21)
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

## Open issues / PRs (2026-09-16 snapshot via gh)
- Open issues: 0
- Open PRs: 1
  - PR #21: feat(ui): apply Prawn UI visual style — NeoCard, NeoButton, Space Grotesk font
    https://github.com/hongyime/theprawnhunter/pull/21
    Branch: maintenance/prawn-ui-20260916 → main

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
