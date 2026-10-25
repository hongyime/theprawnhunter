-- Migration: operator-gated read path for watchlist hits
-- Purpose:
--   watchlist_hits rows are written by the evaluator (dedupe + cooldown) and
--   read by the alert lane, but the dashboard had no way to list them. This
--   RPC exposes a newest-first page joined to the owning rule name and the
--   stored message's telegram id, so the UI never touches the raw tables.
--
--   A deleted rule leaves its historical hits orphaned only when the cascade
--   did not remove them (for example, rows restored from a backup). The LEFT
--   JOIN therefore returns rule_name = NULL instead of dropping the hit.
--
-- Access:
--   SECURITY DEFINER with a fixed search_path, gated by the same
--   public.is_dashboard_operator() predicate as the other watchlist RPCs.
--   Execute is granted to authenticated (and the trusted service role) only;
--   anon and PUBLIC are denied. Role statements are guarded so the migration
--   also applies on vanilla Postgres.
--
-- Idempotent — CREATE OR REPLACE plus guarded REVOKE/GRANT.

CREATE OR REPLACE FUNCTION public.list_watchlist_hits(lim INTEGER DEFAULT 50)
RETURNS TABLE (
    hit_id UUID,
    rule_id UUID,
    rule_name TEXT,
    message_id UUID,
    credential_id UUID,
    matched_text TEXT,
    telegram_msg_id BIGINT,
    created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
    capped INTEGER;
BEGIN
    IF NOT public.is_dashboard_operator() THEN
        RAISE EXCEPTION 'Operator authorization required' USING ERRCODE = '42501';
    END IF;

    capped := LEAST(GREATEST(COALESCE(lim, 50), 1), 200);

    RETURN QUERY
    SELECT
        hits.id AS hit_id,
        hits.rule_id,
        rules.name AS rule_name,
        hits.message_id,
        hits.credential_id,
        hits.matched_text,
        messages.telegram_msg_id,
        hits.created_at
    FROM public.watchlist_hits AS hits
    LEFT JOIN public.watchlist_rules AS rules
        ON rules.id = hits.rule_id
    LEFT JOIN public.exfiltrated_messages AS messages
        ON messages.id = hits.message_id
    ORDER BY hits.created_at DESC
    LIMIT capped;
END
$function$;

REVOKE ALL ON FUNCTION public.list_watchlist_hits(integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION public.list_watchlist_hits(integer) FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.list_watchlist_hits(integer) TO authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.list_watchlist_hits(integer) TO service_role';
    END IF;
END $$;

COMMENT ON FUNCTION public.list_watchlist_hits(integer) IS
  'Operator-only: newest watchlist hits with rule name and telegram message id; limit clamped to 1..200.';
