-- Migration: watchlist rules and hits
-- Purpose:
--   Operators define literal or regex patterns that are evaluated against
--   captured message content. Matches are recorded in watchlist_hits so the
--   alert lane can deduplicate on (rule_id, message_id) and enforce cooldowns.
--
-- Access: RLS enabled with no anon/authenticated policies. Dashboard reads and
-- writes go through the operator-gated SECURITY DEFINER RPCs below. The Python
-- evaluator uses the service key and therefore keeps full table access.
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS public.watchlist_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    is_regex BOOLEAN NOT NULL DEFAULT FALSE,
    credential_id UUID,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    cooldown_minutes INTEGER NOT NULL DEFAULT 60,
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT watchlist_rules_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT watchlist_rules_pattern_length CHECK (char_length(pattern) BETWEEN 1 AND 200),
    CONSTRAINT watchlist_rules_cooldown_min CHECK (cooldown_minutes >= 1)
);

-- Hits are keyed by rule and message so a re-evaluation cannot double-alert.
CREATE TABLE IF NOT EXISTS public.watchlist_hits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id UUID NOT NULL REFERENCES public.watchlist_rules(id) ON DELETE CASCADE,
    message_id UUID NOT NULL,
    credential_id UUID,
    matched_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT watchlist_hits_rule_message_key UNIQUE (rule_id, message_id)
);

-- Recent-hits reads for alerting/metrics lanes.
CREATE INDEX IF NOT EXISTS idx_watchlist_hits_created_at
    ON public.watchlist_hits (created_at DESC);

-- Keep updated_at truthful for any writer, not only the RPCs.
CREATE OR REPLACE FUNCTION public.watchlist_rules_touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS watchlist_rules_touch_updated_at
    ON public.watchlist_rules;
CREATE TRIGGER watchlist_rules_touch_updated_at
BEFORE UPDATE ON public.watchlist_rules
FOR EACH ROW EXECUTE FUNCTION public.watchlist_rules_touch_updated_at();

-- Reuses the single operator helper defined by
-- 20260912121145_restrict_dashboard_operator_access.sql. No second helper.
CREATE OR REPLACE FUNCTION public.list_watchlist_rules()
RETURNS SETOF public.watchlist_rules
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
BEGIN
    IF NOT public.is_dashboard_operator() THEN
        RAISE EXCEPTION 'Operator authorization required' USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT rules.*
    FROM public.watchlist_rules AS rules
    ORDER BY rules.created_at DESC;
END
$function$;

-- p_id NULL inserts; otherwise updates the existing row and raises when it is
-- missing instead of silently inserting. Patterns are capped at 200 chars and
-- cooldowns must be at least one minute.
CREATE OR REPLACE FUNCTION public.upsert_watchlist_rule(
    p_id UUID DEFAULT NULL,
    p_name TEXT DEFAULT NULL,
    p_pattern TEXT DEFAULT NULL,
    p_is_regex BOOLEAN DEFAULT FALSE,
    p_credential_id UUID DEFAULT NULL,
    p_enabled BOOLEAN DEFAULT TRUE,
    p_cooldown_minutes INTEGER DEFAULT 60
)
RETURNS public.watchlist_rules
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
    rule_row public.watchlist_rules;
BEGIN
    IF NOT public.is_dashboard_operator() THEN
        RAISE EXCEPTION 'Operator authorization required' USING ERRCODE = '42501';
    END IF;

    IF p_name IS NULL OR btrim(p_name) = '' THEN
        RAISE EXCEPTION 'Watchlist rule name is required' USING ERRCODE = '22023';
    END IF;

    IF p_pattern IS NULL OR char_length(p_pattern) = 0 THEN
        RAISE EXCEPTION 'Watchlist rule pattern is required' USING ERRCODE = '22023';
    END IF;

    IF char_length(p_pattern) > 200 THEN
        RAISE EXCEPTION 'Watchlist rule pattern exceeds 200 characters' USING ERRCODE = '22001';
    END IF;

    IF p_cooldown_minutes IS NULL OR p_cooldown_minutes < 1 THEN
        RAISE EXCEPTION 'Watchlist cooldown must be at least 1 minute' USING ERRCODE = '22023';
    END IF;

    IF p_id IS NULL THEN
        INSERT INTO public.watchlist_rules (
            name, pattern, is_regex, credential_id, enabled,
            cooldown_minutes, created_by
        ) VALUES (
            btrim(p_name), p_pattern, COALESCE(p_is_regex, FALSE),
            p_credential_id, COALESCE(p_enabled, TRUE),
            p_cooldown_minutes, auth.uid()
        )
        RETURNING * INTO rule_row;
    ELSE
        UPDATE public.watchlist_rules
        SET name = btrim(p_name),
            pattern = p_pattern,
            is_regex = COALESCE(p_is_regex, FALSE),
            credential_id = p_credential_id,
            enabled = COALESCE(p_enabled, TRUE),
            cooldown_minutes = p_cooldown_minutes,
            updated_at = NOW()
        WHERE id = p_id
        RETURNING * INTO rule_row;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'Watchlist rule not found' USING ERRCODE = 'P0002';
        END IF;
    END IF;

    RETURN rule_row;
END
$function$;

CREATE OR REPLACE FUNCTION public.delete_watchlist_rule(p_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
BEGIN
    IF NOT public.is_dashboard_operator() THEN
        RAISE EXCEPTION 'Operator authorization required' USING ERRCODE = '42501';
    END IF;

    DELETE FROM public.watchlist_rules WHERE id = p_id;
    RETURN FOUND;
END
$function$;

-- Security: RLS with no policies denies anon/authenticated table access. Role
-- statements are guarded so the migration also applies on vanilla Postgres.
DO $$
BEGIN
    IF to_regclass('public.watchlist_rules') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.watchlist_rules ENABLE ROW LEVEL SECURITY';
        EXECUTE 'REVOKE ALL ON public.watchlist_rules FROM PUBLIC';
    END IF;
    IF to_regclass('public.watchlist_hits') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.watchlist_hits ENABLE ROW LEVEL SECURITY';
        EXECUTE 'REVOKE ALL ON public.watchlist_hits FROM PUBLIC';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON public.watchlist_rules FROM anon';
        EXECUTE 'REVOKE ALL ON public.watchlist_hits FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON public.watchlist_rules FROM authenticated';
        EXECUTE 'REVOKE ALL ON public.watchlist_hits FROM authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT ALL ON public.watchlist_rules TO service_role';
        EXECUTE 'GRANT ALL ON public.watchlist_hits TO service_role';
    END IF;
END $$;

REVOKE ALL ON FUNCTION public.list_watchlist_rules() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.upsert_watchlist_rule(uuid, text, text, boolean, uuid, boolean, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.delete_watchlist_rule(uuid) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION public.list_watchlist_rules() FROM anon';
        EXECUTE 'REVOKE ALL ON FUNCTION public.upsert_watchlist_rule(uuid, text, text, boolean, uuid, boolean, integer) FROM anon';
        EXECUTE 'REVOKE ALL ON FUNCTION public.delete_watchlist_rule(uuid) FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.list_watchlist_rules() TO authenticated';
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.upsert_watchlist_rule(uuid, text, text, boolean, uuid, boolean, integer) TO authenticated';
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.delete_watchlist_rule(uuid) TO authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.list_watchlist_rules() TO service_role';
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.upsert_watchlist_rule(uuid, text, text, boolean, uuid, boolean, integer) TO service_role';
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.delete_watchlist_rule(uuid) TO service_role';
    END IF;
END $$;

COMMENT ON TABLE public.watchlist_rules IS
  'Operator-defined literal/regex patterns evaluated against captured messages.';
COMMENT ON TABLE public.watchlist_hits IS
  'One row per (rule, message) match; dedupe and cooldown state for alerting.';
COMMENT ON FUNCTION public.list_watchlist_rules() IS
  'Operator-only: list watchlist rules newest first.';
COMMENT ON FUNCTION public.upsert_watchlist_rule(uuid, text, text, boolean, uuid, boolean, integer) IS
  'Operator-only: insert (p_id NULL) or update a watchlist rule; rejects >200-char patterns and cooldowns below 1 minute.';
COMMENT ON FUNCTION public.delete_watchlist_rule(uuid) IS
  'Operator-only: delete a watchlist rule; returns whether a row was removed.';
