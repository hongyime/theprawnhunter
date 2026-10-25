-- Migration: operator-only realtime broadcast on the private `evidence` channel
-- Purpose:
--   The dashboard subscribes to the private `evidence` broadcast channel and
--   refetches the newest evidence page when a `{credential_id, message_id}`
--   delta arrives. Supabase Realtime authorization for private channels is
--   enforced by RLS on `realtime.messages`: a client may only read a topic
--   when a SELECT policy allows the corresponding `realtime.messages` row.
--
-- Shape follows the documented Supabase private-channel authorization policy:
--   https://supabase.com/docs/guides/realtime/authorization
--   (this project has no prior `realtime.messages` policy to copy).
--
-- Access: authenticated operators only (`public.is_dashboard_operator()`),
-- broadcast extension only, topic `evidence` only. No anon/PUBLIC policy.
--
-- Idempotent and safe on vanilla Postgres: every realtime-specific statement is
-- guarded because the `realtime` schema and its `messages` table do not exist
-- outside Supabase. Re-running drops and recreates only this policy.

DO $migration$
BEGIN
    IF to_regclass('realtime.messages') IS NULL THEN
        RAISE NOTICE 'realtime.messages not present; evidence broadcast policy skipped';
        RETURN;
    END IF;

    -- The operator helper is defined by
    -- 20260912121145_restrict_dashboard_operator_access.sql. Skip rather than
    -- fail when this migration is replayed against a partial fixture database.
    IF to_regprocedure('public.is_dashboard_operator()') IS NULL THEN
        RAISE NOTICE 'public.is_dashboard_operator() not present; evidence broadcast policy skipped';
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE realtime.messages ENABLE ROW LEVEL SECURITY';

    -- Roles are Supabase-managed; guard so vanilla Postgres fixtures still apply.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        RAISE NOTICE 'role authenticated not present; evidence broadcast policy skipped';
        RETURN;
    END IF;

    EXECUTE 'DROP POLICY IF EXISTS evidence_broadcast_operator_read ON realtime.messages';

    -- SELECT on realtime.messages is the authorization check Realtime performs
    -- before delivering a private broadcast to a subscriber.
    EXECUTE $policy$
        CREATE POLICY evidence_broadcast_operator_read
        ON realtime.messages
        FOR SELECT
        TO authenticated
        USING (
            realtime.messages.extension = 'broadcast'
            AND realtime.messages.topic = 'evidence'
            AND public.is_dashboard_operator()
        )
    $policy$;

    -- Realtime authorization reads this table as the subscribing role. Ensure
    -- authenticated can evaluate the policy while anon/PUBLIC remain denied.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON realtime.messages FROM anon';
    END IF;
    EXECUTE 'GRANT SELECT ON realtime.messages TO authenticated';

    EXECUTE $comment$
        COMMENT ON POLICY evidence_broadcast_operator_read ON realtime.messages IS
          'Operator-only private broadcast on the evidence topic; payloads carry ids only.'
    $comment$;
END
$migration$;
