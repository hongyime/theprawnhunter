-- Migration: full-text search over captured evidence
-- Purpose:
--   Analysts need to search the captured message corpus instead of paging
--   through a feed. This migration adds a maintained tsvector column on the
--   base table (public.exfiltrated_messages) and an operator-gated RPC that
--   returns highlighted snippets.
--
-- Why the base table:
--   public.evidence_redacted is a view. A view cannot own a stored generated
--   column or a GIN index over one, so the vector and index live on the base
--   table. The view's explicit column list is unaffected by the extra column.
--
-- Storage / cost:
--   search_vector is a plain (non-generated) tsvector column so the value is
--   reused by the GIN index and ts_headline queries. It is maintained by a
--   BEFORE trigger using to_tsvector('simple', coalesce(content, '')).
--   'simple' is used because the corpus is multilingual and stemmers are
--   language-specific; it lowercases and splits without stemming.
--
-- Backfill:
--   Existing rows are filled in 5,000-row UPDATE batches, only where
--   search_vector IS NULL, so re-running is cheap and a partial run resumes.
--   Note: a DO block runs as one transaction, so row locks taken by the
--   batches are released together at commit. The batching keeps each
--   statement's memory/WAL footprint bounded and avoids one whole-table
--   rewrite, but it is not a substitute for an online rewrite.
--
-- Index build:
--   CREATE INDEX (non-concurrent) takes a SHARE lock, blocking writes to
--   exfiltrated_messages for the duration of the build. That is intentional:
--   the migration runner does not support CONCURRENTLY. Schedule the apply in
--   a quiet window and check the table size first.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, CREATE OR REPLACE FUNCTION,
-- DROP TRIGGER IF EXISTS, NULL-only backfill, CREATE INDEX IF NOT EXISTS.
-- Role statements are guarded so the migration also applies on a vanilla
-- Postgres without Supabase's roles.

ALTER TABLE public.exfiltrated_messages
    ADD COLUMN IF NOT EXISTS search_vector tsvector;

-- Keeps search_vector in sync for every writer (application, psql, backfills
-- that rewrite content). UPDATE OF content limits the trigger to the only
-- column that affects the vector; the migration's own backfill UPDATE sets
-- search_vector directly and therefore does not re-fire it.
CREATE OR REPLACE FUNCTION public.evidence_search_vector_update()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    NEW.search_vector := to_tsvector('simple', COALESCE(NEW.content, ''));
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS evidence_search_vector_update
    ON public.exfiltrated_messages;
CREATE TRIGGER evidence_search_vector_update
BEFORE INSERT OR UPDATE OF content ON public.exfiltrated_messages
FOR EACH ROW EXECUTE FUNCTION public.evidence_search_vector_update();

-- Batched backfill: only NULL vectors, 5,000 rows per statement. Rows inserted
-- after the trigger exists are already populated and are skipped.
DO $$
DECLARE
    batch_size CONSTANT INTEGER := 5000;
    rows_updated INTEGER;
BEGIN
    LOOP
        UPDATE public.exfiltrated_messages AS m
        SET search_vector = to_tsvector('simple', COALESCE(m.content, ''))
        WHERE m.id IN (
            SELECT pending.id
            FROM public.exfiltrated_messages AS pending
            WHERE pending.search_vector IS NULL
            ORDER BY pending.id
            LIMIT batch_size
        );

        GET DIAGNOSTICS rows_updated = ROW_COUNT;
        EXIT WHEN rows_updated = 0;

        RAISE NOTICE 'evidence_fts backfill: % rows', rows_updated;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_exfiltrated_messages_search_vector
    ON public.exfiltrated_messages USING GIN (search_vector);

-- Result row shape. A named composite type is used instead of RETURNS TABLE
-- because the RPC's input parameter is also called credential_id (the JSON key
-- clients send); RETURNS TABLE would declare a second parameter with that name
-- and PL/pgSQL rejects duplicate parameter names.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        WHERE t.typname = 'evidence_search_result'
          AND n.nspname = 'public'
    ) THEN
        CREATE TYPE public.evidence_search_result AS (
            id UUID,
            credential_id UUID,
            sender_key TEXT,
            sender_pseudonym TEXT,
            snippet TEXT,
            media_type TEXT,
            created_at TIMESTAMPTZ
        );
    END IF;
END $$;

-- Operator-gated search. Reuses public.is_dashboard_operator() (the single
-- authorization helper from 20260912121145_restrict_dashboard_operator_access.sql)
-- and raises 42501 for non-operators, matching the other dashboard RPCs.
--
-- Snippet: ts_headline('simple', content, query) -> matched terms wrapped in
-- <b>...</b> and a short window around them.
-- Filter: credential_id narrows results only when the argument is non-NULL.
-- Clamp: lim is coerced to 1..200 (NULL -> 50).
-- Invalid query text must never raise: websearch_to_tsquery is guarded, with
-- plainto_tsquery and then an empty result as fallbacks.
CREATE OR REPLACE FUNCTION public.search_evidence(
    q TEXT,
    credential_id UUID DEFAULT NULL,
    lim INTEGER DEFAULT 50
)
RETURNS SETOF public.evidence_search_result
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
    v_query tsquery;
    v_limit INTEGER;
    v_credential_id UUID;
BEGIN
    IF NOT public.is_dashboard_operator() THEN
        RAISE EXCEPTION 'Operator authorization required' USING ERRCODE = '42501';
    END IF;

    v_limit := LEAST(GREATEST(COALESCE(lim, 50), 1), 200);
    v_credential_id := credential_id;

    BEGIN
        v_query := websearch_to_tsquery('simple', COALESCE(q, ''));
    EXCEPTION WHEN OTHERS THEN
        BEGIN
            v_query := plainto_tsquery('simple', COALESCE(q, ''));
        EXCEPTION WHEN OTHERS THEN
            v_query := NULL;
        END;
    END;

    -- NULL/zero-lexeme tsquery matches nothing; return no rows rather than
    -- scanning. numnode() avoids parsing an empty tsquery literal, which would
    -- emit a "doesn't contain lexemes" notice on every call.
    IF v_query IS NULL OR numnode(v_query) = 0 THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        m.id,
        m.credential_id,
        'user_'::text || "substring"(
            md5(COALESCE(m.sender_user_id::text, m.sender_name, '') || 'salt_pr4wn_hunt3r'::text),
            1, 8
        ) AS sender_key,
        'user_'::text || "substring"(
            md5(m.sender_name || 'salt_pr4wn_hunt3r'::text),
            1, 8
        ) AS sender_pseudonym,
        ts_headline('simple', COALESCE(m.content, ''), v_query) AS snippet,
        m.media_type,
        m.created_at
    FROM public.exfiltrated_messages AS m
    WHERE m.search_vector @@ v_query
      AND (v_credential_id IS NULL OR m.credential_id = v_credential_id)
    ORDER BY m.created_at DESC
    LIMIT v_limit;
END
$function$;

-- ACL: PUBLIC and anon stay denied; authenticated may execute (the function
-- itself still raises for non-operators). service_role is granted because the
-- worker/API use the service key and function privileges are checked even for
-- BYPASSRLS roles.
REVOKE ALL ON FUNCTION public.search_evidence(text, uuid, integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION public.search_evidence(text, uuid, integer) FROM anon';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.search_evidence(text, uuid, integer) TO authenticated';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.search_evidence(text, uuid, integer) TO service_role';
    END IF;
END $$;

COMMENT ON FUNCTION public.search_evidence(text, uuid, integer) IS
  'Operator-only full-text evidence search. Returns highlighted snippets from public.exfiltrated_messages, newest first. lim clamps to 1..200; credential_id filters when non-NULL; malformed query text falls back instead of raising.';
COMMENT ON FUNCTION public.evidence_search_vector_update() IS
  'BEFORE INSERT OR UPDATE OF content trigger helper: maintains exfiltrated_messages.search_vector with to_tsvector(''simple'', coalesce(content, '''')).';
COMMENT ON COLUMN public.exfiltrated_messages.search_vector IS
  'Full-text vector for evidence search, maintained by evidence_search_vector_update() and searched by public.search_evidence().';
