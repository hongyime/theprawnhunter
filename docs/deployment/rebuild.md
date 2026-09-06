# Deployment Rebuild Runbook — 2026-09-06 remediation cycle

**Branch:** `remediation/2026-09-06`
**Cycle:** 02_EXECUTE — 69 findings, 46 tasks
**Operator delegation:** operator asked the agent to perform `docker compose build` + `up -d`. Runbook below documents the sequence for reproducibility and for the Vercel side which the agent does not touch.

---

## 1. Migrations to apply (Supabase SQL editor or `supabase db push`)

Apply **in this exact order**:

| Order | File | Purpose |
|---|---|---|
| 1 | `supabase/migrations/20260906000006_verify_pending_migrations.sql` | Non-DDL check. Raises if `findings`, `finding_evidence`, `engagement_events`, `honeypot_redirect_log` are absent — i.e. if DATA-001 has not been fully resolved via applying `20260806000001`, `20260904000002`, `20260904000003`, `20260904000004`, `20260904000005`, `20260906000001`. Run these FIRST if the check raises. |
| 2 | `supabase/migrations/20260906000003_broadcast_reliability_ext.sql` | Adds `broadcast_message_id BIGINT` and `broadcast_status TEXT` on `exfiltrated_messages`. Backfills existing rows so `broadcast_status='sent'` where `is_broadcasted=true`, `'pending'` otherwise. Adds partial index `idx_messages_permanent_failed`. Idempotent, additive. |
| 3 | `supabase/migrations/20260906000004_media_hashes_failure_flag.sql` | (Ships Session B, T12) Adds `is_failure BOOLEAN, failure_reason TEXT` to `media_hashes`; migrates existing `sha256='__failed__%'` sentinel rows; adds `UNIQUE(message_id)`. See INTR-005 / CONC-003. |
| 4 | `supabase/migrations/20260906000005_audit_logs_composite_idx.sql` | (Ships Session B, T13) `CREATE INDEX CONCURRENTLY idx_audit_event_type_timestamp ON audit_logs(event_type, timestamp DESC)`. See PERF-001. |

Each file has a `-- ROLLBACK` block at the bottom for reverse-DDL.

**Note on SEC-001**: operator explicitly opted out of the plaintext-token remediation (`bugfix.md § SEC-001`). Migration `20260906000002_deprecate_extension_direct_write.sql` is NOT shipped. Extension continues writing raw. The 478 historical plaintext rows remain as-is.

---

## 2. Docker rebuild sequence

The agent will run these on the operator's machine when Session E closes:

```powershell
# Pre-rebuild sanity — none of these should be pending
docker compose ps --format 'table {{.Name}}\t{{.Status}}'

# Rebuild all backend services from HEAD of remediation/2026-09-06
docker compose build --no-cache api bot worker-core worker-scanners worker-scrape worker-validators beat flower frontend

# Roll all containers atomically. This does NOT touch volumes.
docker compose up -d

# Verify each backend service is healthy within 3 minutes
Start-Sleep -Seconds 60
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
```

Expected: all `theprawnhunter_*` containers show `Up (healthy)` for services with healthchecks (`api`, `bot`, `redis`, all 4 workers, `beat`); `flower` and `frontend` show `Up` and healthchecks post-T20 (see below).

If any container is not healthy after 3 minutes, stop and rollback (§4).

---

## 3. Frontend deploy (Vercel — operator owns)

The Vercel project must also be redeployed against `origin/main` (once the remediation branch is merged) so the RLS-hardening for `discovered_credentials_public` (grant to `authenticated` only) is enforced on the customer-facing surface.

- The agent does not have Vercel credentials.
- Operator runs `vercel --prod` from `frontend/` or clicks "Redeploy" in the Vercel dashboard once the merged main is pushed.
- Post-deploy: verify unauthenticated `curl` to `discovered_credentials_public` returns 401 or empty payload.

---

## 4. Rollback

If a container crashes or the migrations cause data issues:

```powershell
# Stop the new stack — this does NOT wipe volumes (no -v)
docker compose down

# Roll back the code
git checkout main   # or: git reset --hard <pre-cycle-SHA>

# Rebuild + restart from pre-cycle images
docker compose build api bot worker-core worker-scanners worker-scrape worker-validators beat flower frontend
docker compose up -d
```

Migration rollback: each shipped `.sql` file has a `-- ROLLBACK` block. Paste the block into the Supabase SQL editor to reverse DDL, in reverse order of application.

---

## 5. Post-deploy verification checklist

- [ ] `curl http://localhost:8011/health/` returns `{"status":"ok"}` or 200 body.
- [ ] `curl -H "X-Monitor-Key: $MONITOR_API_KEY" http://localhost:8011/health/detailed` reports DB=up, Redis=up, Bot API=reachable.
- [ ] `curl -H "X-Monitor-Key: $MONITOR_API_KEY" http://localhost:8011/health/queues` shows queue depths.
- [ ] `curl -H "X-Monitor-Key: $MONITOR_API_KEY" http://localhost:8011/monitor/findings` returns `[]` (was 500 before DATA-001 apply — if still 500, DATA-001 not resolved).
- [ ] `docker inspect theprawnhunter_flower --format '{{.State.Health.Status}}'` returns `healthy` (after Session B T20 ships the healthcheck).
- [ ] `docker inspect theprawnhunter_frontend --format '{{.State.Health.Status}}'` returns `healthy`.
- [ ] Sample: `docker exec theprawnhunter_worker-core python -c "from app.core.database import db; r=db.table('exfiltrated_messages').select('id',count='exact').eq('broadcast_status','permanent_failed').limit(0).execute(); print('permanent_failed:',r.count)"` — number visible.

---

## 6. Cycle-specific migration mapping

| Migration | Task | Finding |
|---|---|---|
| `20260906000003_broadcast_reliability_ext.sql` | T03 | INTR-001, DATA-003, REL-003 |
| `20260906000004_media_hashes_failure_flag.sql` | T12 (Session B) | INTR-005, CONC-003 |
| `20260906000005_audit_logs_composite_idx.sql` | T13 (Session B) | PERF-001 |
| `20260906000006_verify_pending_migrations.sql` | T02 | DATA-001 |
| **NOT SHIPPED**: `20260906000002_deprecate_extension_direct_write.sql` | T04 (deferred) | SEC-001 — operator accepts plaintext |
