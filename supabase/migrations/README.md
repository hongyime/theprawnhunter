# Database migration authority

`supabase/migrations/` is the sole chronological schema-change stream. Add new
schema changes here as timestamped SQL migrations and apply them through the
project's reviewed Supabase deployment process.

`database/init.sql` and `database/rls_policies.sql` are reference snapshots,
not alternate migration sources. Pre-Supabase manual patches are retained in
`docs/history/legacy_migrations/` for historical reference and must not be
replayed as current migrations.
