# Task List — Current Backlog

**Updated:** 2026-07-15  
**Source of truth:** current repo + live Docker stack + runtime verification  
**Status model:** `open`, `in_progress`, `done`, `deployed`

---

## P0 — Runtime breakages

### P0-001 — DB health probe returns false 503s
**Status:** done  
**Files:** `app/core/db_retry.py`, `app/api/routers/health.py`, `scripts/validate_deployment.py`, `scripts/validate_startup.py`  
**Issue:** `with_db_retry()` did not return its wrapper, so `DatabaseHealth.check_connection` became `None` and `/health/detailed` degraded incorrectly.  
**Fix:** decorator now returns the wrapped function; validation scripts now assert the health probe remains callable.

### P0-002 — Bot listener cold-cache async crash
**Status:** done  
**Files:** `app/services/bot_listener.py`, `app/services/_scraper/monitor_guard.py`  
**Issue:** `log_update()` called sync `_is_monitor_group()` from async context, triggering a cold-cache runtime error.  
**Fix:** `log_update()` now resolves monitor IDs through `_resolve_monitor_group_ids_async()` and compares locally.

### P0-003 — Flower service crash-loop
**Status:** done  
**Files:** `requirements.txt`, `docker-compose.yml`  
**Issue:** Compose starts `celery ... flower`, but the package was not present in the image.  
**Fix:** backend requirements now include `flower==2.0.1`. Rebuild required for running containers.

### P0-004 — confidence_score drift across environments
**Status:** done  
**Files:** `database/init.sql`, `database/migrations/2026-07-15-canonicalize-confidence-score-generated.sql`, `app/workers/tasks/validation_tasks.py`, `scripts/validate_deployment.py`, `scripts/validate_startup.py`  
**Issue:** some environments still carry the older writable `confidence_score` column shape, while the canonical schema is generated-from-`meta`. Older validator code also tried to update the top-level column directly.  
**Fix:** added a canonicalizing migration for legacy environments; validation scripts assert the backfill path only updates `meta`. Rebuild required for any stale worker image.

### P0-005 — Frontend release drift exposes findings without auth
**Status:** open  
**Severity:** HIGH (security)  
**Diagnosis:** 207bf47  
**Issue:** Anonymous Vercel deployment + local frontend expose findings data because both run old SHA 84e2e47. Current source already has auth/RLS fixes but deployments not updated.  
**Impact:** Unauthenticated users can access `/monitor/findings` endpoint and view all discovered credentials.  
**Fix Required:** (1) Push 22+ commits to origin/main, (2) Trigger Vercel rebuild, (3) Rebuild local Docker frontend container, (4) QA verifies fresh-session gate blocks anonymous access.  
**Blocking:** Production-code changes must wait until frontend is redeployed with auth fixes.  

---

## P1 — Guardrails

### P1-001 — Deployment validation covers recent regressions
**Status:** done  
**Files:** `scripts/validate_deployment.py`  
**Checks added:**
- health probe remains callable
- bot listener uses async monitor guard path
- validator backfill does not update top-level `confidence_score`

### P1-002 — Startup validation covers recent regressions
**Status:** done  
**Files:** `scripts/validate_startup.py`  
**Checks added:**
- health probe callable check
- async monitor-group guard check
- validator backfill payload shape check

---

## P2 — Backlog hygiene

### P2-001 — Replace stale remediation ledger
**Status:** done  
**Files:** `tasks.md`  
**Issue:** the previous file described already-completed 2026-04 remediation work as pending, which made it unusable for current operations.  
**Fix:** replaced it with the current verified backlog and status notes.

### P2-002 — Remove duplicate roadmap noise from active backlog
**Status:** done  
**Files:** `tasks.md`  
**Issue:** the active task list mixed implemented work with future ideas like scanner expansion.  
**Fix:** active backlog now tracks only current verified runtime and operational work.

---

## Deployment note

The live stack must be rebuilt for backend image changes to take effect:

```powershell
docker compose build api bot worker-validators flower
docker compose up -d api bot worker-validators flower
```

If you want all backend services on the same image revision, rebuild and recreate `worker-core`, `worker-scanners`, `worker-scrape`, and `beat` as well.
