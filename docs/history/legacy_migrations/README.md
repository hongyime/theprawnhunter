# Database Migrations

Two folders — one canonical source of truth, one CLI-tracked.

## `database/migrations/` (this folder)

**Human-readable historical record.** Files named `YYYY-MM-DD-description.sql`.
Everything ever run against the Supabase DB lives here. Newest migrations
should also be mirrored into `supabase/migrations/` (see below).

## `supabase/migrations/` (CLI-tracked)

**Machine-tracked by Supabase CLI.** Files must be named `YYYYMMDDHHMMSS_snake_case.sql`
per Supabase CLI convention.

## Workflow for a new migration

1. Write the SQL. Use `IF NOT EXISTS` / `IF EXISTS` guards for idempotency.
2. Save two copies:
   - `database/migrations/YYYY-MM-DD-description.sql` (human-friendly)
   - `supabase/migrations/YYYYMMDDHHMMSS_description.sql` (CLI-friendly)
3. Apply:
   ```powershell
   supabase db push
   ```
4. Verify:
   ```powershell
   supabase migration list
   ```

## Baselining (one-time, first push)

Migrations already applied manually via the Dashboard SQL Editor need to be
marked as applied so `supabase db push` doesn't try to re-run them:

```powershell
supabase migration repair --status applied 20260802000001
```

Add one `repair` call per historical migration in `supabase/migrations/`.
