# Supabase Keepalive Setup

Supabase's free tier pauses inactive projects after 7 days. The `keepalive_log` table + GitHub Actions workflow keep the project active with a lightweight daily write.

**Canonical table name is `keepalive_log` (singular)**. Historical drafts said `keepalive_logs` — DRIFT-007 in the 2026-09-06 remediation cycle corrected this.

---

## Schema

Defined in `database/init.sql`:

```sql
CREATE TABLE IF NOT EXISTS keepalive_log (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    status     TEXT        DEFAULT 'ok'
);
```

Grants required for the anon role (see `database/migrations/001_keepalive_grant.sql`):

```sql
GRANT INSERT, DELETE ON keepalive_log TO anon;
```

RLS: table can remain unrestricted for anon since the row content is trivial. The DELETE grant lets the workflow prune rows older than 7 days.

---

## Workflow

`.github/workflows/supabase-keep-alive.yml` — runs daily at 08:00 UTC. Uses two GitHub secrets:

- `SUPABASE_URL` — project URL, e.g. `https://<ref>.supabase.co`.
- `SUPABASE_KEY` — anon key (do NOT use service_role for this — the anon key exercises the REST gateway, which is what Supabase's inactivity monitor watches).

Workflow steps:

1. `curl -X POST` an insert with `status: 'ok'` payload — expect HTTP 201.
2. `curl -X DELETE` any rows older than 7 days — expect HTTP 204.

If the INSERT returns 403 or 401, the grants haven't been applied. Re-run `database/migrations/001_keepalive_grant.sql` in the Supabase SQL editor.

---

## Verification

```sql
SELECT COUNT(*), MAX(created_at) FROM keepalive_log;
```

At steady state there are ≤ 7 rows (one per day, oldest pruned).

---

## Manual trigger

If the automation fails and the project is about to pause:

```bash
curl -X POST "$SUPABASE_URL/rest/v1/keepalive_log" \
    -H "apikey: $SUPABASE_ANON_KEY" \
    -H "Authorization: Bearer $SUPABASE_ANON_KEY" \
    -H "Content-Type: application/json" \
    -H "Prefer: return=minimal" \
    -d '{"status":"manual"}'
```

Response: `HTTP 201`. Project inactivity timer resets.
