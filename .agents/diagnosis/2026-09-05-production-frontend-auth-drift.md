# Production frontend authentication drift

**Date:** 2026-09-05  
**Status:** Confirmed  
**Severity:** High  
**Scope:** Vercel production alias and the local frontend container

## Symptom contract

An anonymous browser with no Supabase session must see the sign-in gate. Findings, chat content,
telemetry, and credential metadata must be unavailable until authentication succeeds.

## Evidence

Using a fresh `agent-browser` session against `https://theprawnhunter.vercel.app/`, then clearing
cookies, local storage, and session storage before reloading, still rendered the findings dashboard.
The same reproduction succeeds against `http://127.0.0.1:3000/`.

GitHub's latest successful Vercel Production deployment is deployment `6224018022`, created
2026-09-02, from SHA `84e2e47ff0251cf8d7b0009c900bc92897f8be86`. That SHA is `origin/main`.
The authentication gate and RLS hardening commits (`5a20acb`, `b6194c4`, `70b35c2`) are among the
local commits ahead of `origin/main`, so neither deployed frontend includes them yet.

## Root cause

Deployment state is behind the reviewed local branch. The local frontend container is likewise an
older build. This is release drift, not a missing authentication check in the current source.

## Fix

1. Complete the release gate for the entire `origin/main..HEAD` diff.
2. Fast-forward and push `main` without force.
3. Confirm Vercel creates a successful Production deployment for the pushed SHA.
4. Rebuild/restart the local frontend from that SHA using the documented production procedure.

## Verification plan

- In a clean browser session, both the production alias and local frontend show only the sign-in
  gate and `/signin` is usable.
- Authenticated sessions can still open findings, chat, per-bot telemetry, and global telemetry.
- Browser console and page-error logs remain empty during both anonymous and authenticated flows.
- The Vercel Production deployment SHA equals the pushed `main` SHA.

## Recurrence control

Treat deployed-SHA equality and an anonymous browser auth-gate smoke test as release-gate checks.
The production runbook should record both results for every frontend release.
