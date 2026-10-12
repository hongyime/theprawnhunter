# Archived pre-Supabase migration patches

This directory preserves SQL patches that predate the current Supabase
migration stream. They are historical references only and must not be replayed
as current schema changes.

The sole migration authority is `supabase/migrations/`. Add any new schema
change there as a timestamped migration and deploy it through the reviewed
Supabase process. `database/init.sql` and `database/rls_policies.sql` are
reference snapshots, not competing sources of schema changes.
