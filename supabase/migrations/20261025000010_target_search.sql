-- Migration: operator-gated target search RPC
-- Purpose:
--   The dashboard sidebar loads at most 2000 targets and filters them in the
--   browser. A query of two or more characters now asks Postgres directly so
--   matching targets outside that window are still found. The RPC returns the
--   same row shape as public.discovered_credentials_public so the sidebar can
--   render results without a second fetch or field mapping.
--
-- Access: RPC only. The view and table grants are unchanged; the caller must
-- be an authenticated dashboard operator, checked through the single helper
-- public.is_dashboard_operator() (no second authorization helper).
--
-- Idempotent — CREATE OR REPLACE plus role-guarded ACL statements.

CREATE OR REPLACE FUNCTION public.search_targets(
    q TEXT,
    lim INTEGER DEFAULT 50
)
RETURNS SETOF public.discovered_credentials_public
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
    needle TEXT;
    pattern TEXT;
    row_limit INTEGER;
BEGIN
    IF NOT public.is_dashboard_operator() THEN
        RAISE EXCEPTION 'Operator authorization required' USING ERRCODE = '42501';
    END IF;

    needle := btrim(COALESCE(q, ''));

    -- Escape LIKE metacharacters so a query containing % or _ is matched
    -- literally, mirroring the client's substring search.
    IF needle = '' THEN
        pattern := '%';
    ELSE
        pattern := '%' ||
            replace(
                replace(
                    replace(needle, '\', '\\'),
                    '%', '\%'
                ),
                '_', '\_'
            ) || '%';
    END IF;

    row_limit := LEAST(GREATEST(COALESCE(lim, 50), 1), 200);

    RETURN QUERY
    SELECT targets.*
    FROM public.discovered_credentials_public AS targets
    WHERE COALESCE(targets.meta->>'bot_username', '') ILIKE pattern ESCAPE '\'
       OR COALESCE(targets.meta->>'chat_title', '') ILIKE pattern ESCAPE '\'
       OR COALESCE(targets.chat_name, '') ILIKE pattern ESCAPE '\'
    ORDER BY targets.collection_yield_score DESC NULLS LAST,
             targets.created_at DESC
    LIMIT row_limit;
END
$function$;

-- Security: authenticated operators call the RPC; PUBLIC and anon cannot.
-- Role statements are guarded so the migration also applies on vanilla
-- Postgres without Supabase's roles.
REVOKE ALL ON FUNCTION public.search_targets(text, integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION public.search_targets(text, integer) FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.search_targets(text, integer) TO authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.search_targets(text, integer) TO service_role';
    END IF;
END $$;

COMMENT ON FUNCTION public.search_targets(text, integer) IS
  'Operator-only: case-insensitive search over bot username, chat title and chat name; lim clamped to 1..200; ordered like the sidebar list.';
