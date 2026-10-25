-- Migration: let service_role through the search RPC gates + index the ILIKE fallback
-- Purpose:
--   Live monitor/search runs with the service-role key, but search_evidence()
--   and semantic_search() only authorised public.is_dashboard_operator(). A
--   service-role request has no signed-in operator (auth.uid() IS NULL), so the
--   gate raised 42501 and the intended FTS path never ran in production.
--   This migration adds a service_role branch to both gates without weakening
--   the operator check for anon/authenticated users.
--
-- Gate expression:
--   public.is_dashboard_operator() OR the request's JWT role claim is
--   'service_role'. current_setting('request.jwt.claims', true) is NULL when
--   PostgREST did not set the GUC (plain psql, vanilla Postgres fixtures, the
--   Management API connection), and NULLIF(...) keeps the empty string out of
--   the jsonb cast. COALESCE(...) makes the absent case an empty role rather
--   than an error, so this migration is portable and never fails closed with a
--   cast error.
--
--   Why not auth.role(): it only exists in Supabase's auth schema and would
--   break the vanilla-Postgres test fixtures. Reading the GUC directly is the
--   same value PostgREST/JWT verification places there.
--
-- ILIKE fallback index:
--   The monitor API falls back to `content ILIKE '%q%' OR sender_name ILIKE
--   '%q%'` when the RPC is unavailable. Without a trigram index that is a
--   sequential scan over the whole message corpus (134,495 rows at the time of
--   writing) and exceeds the PostgREST statement timeout (SQLSTATE 57014).
--   Both OR branches need a trigram index for the planner to consider a
--   BitmapOr, so both columns are indexed; a content-only index is not used
--   when the predicate ORs in an unindexed column.
--
--   Build cost: GIN trigram indexes store every 3-gram of the indexed text, so
--   they are typically a few times larger than the column data. CREATE INDEX
--   (non-concurrent) takes a SHARE lock and blocks writes to
--   exfiltrated_messages for the duration of each build; on the free tier this
--   can take tens of seconds per index on ~135k rows. The migration runner is
--   a single transaction with a 120 s client timeout: if it times out, re-run
--   the migration. Every statement is idempotent, and
--   `CREATE INDEX IF NOT EXISTS` continues a previously started build.
--
-- Untouched:
--   Function signatures, return columns, ordering, lim clamps, SECURITY
--   DEFINER, search_path, SECURITY DEFINER ACLs and comments are copied from
--   20261025000003/20261025000009 (search_evidence) and 20261025000007
--   (semantic_search) verbatim; only the authorization predicate differs.
--
-- Idempotent: CREATE EXTENSION IF NOT EXISTS, CREATE INDEX IF NOT EXISTS,
-- CREATE OR REPLACE FUNCTION, role-guarded REVOKE/GRANT.

-- ---------------------------------------------------------------------------
-- 1. Trigram indexes backing the ILIKE fallback
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Resolve gin_trgm_ops in both install layouts: public (vanilla Postgres) and
-- extensions (Supabase's convention).
SET search_path = public, extensions, pg_temp;

CREATE INDEX IF NOT EXISTS idx_exfiltrated_messages_content_trgm
    ON public.exfiltrated_messages USING gin (content gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_exfiltrated_messages_sender_name_trgm
    ON public.exfiltrated_messages USING gin (sender_name gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- 2. search_evidence(): 20261025000009 definition, service_role gate added
-- ---------------------------------------------------------------------------

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
    IF NOT (
        public.is_dashboard_operator()
        OR COALESCE(
             NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role',
             ''
           ) = 'service_role'
    ) THEN
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
        ts_headline(
            'simple',
            COALESCE(m.content, '') || ' ' ||
            COALESCE(m.file_meta->>'transcript_text', '') || ' ' ||
            COALESCE(m.file_meta->>'ocr_text', ''),
            v_query
        ) AS snippet,
        m.media_type,
        m.created_at
    FROM public.exfiltrated_messages AS m
    WHERE m.search_vector @@ v_query
      AND (v_credential_id IS NULL OR m.credential_id = v_credential_id)
    ORDER BY m.created_at DESC
    LIMIT v_limit;
END
$function$;

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
  'Operator-or-service-role full-text evidence search over content + file_meta transcript/OCR. Returns highlighted snippets from public.exfiltrated_messages, newest first. lim clamps to 1..200; credential_id filters when non-NULL; malformed query text falls back instead of raising.';

-- ---------------------------------------------------------------------------
-- 3. semantic_search(): 20261025000007 definition, service_role gate added
-- ---------------------------------------------------------------------------

-- Extension and type must resolve in the session that creates the function.
CREATE EXTENSION IF NOT EXISTS vector;
SET search_path = public, extensions, pg_temp;

CREATE OR REPLACE FUNCTION public.semantic_search(
    query_embedding vector(384),
    credential_id UUID DEFAULT NULL,
    lim INTEGER DEFAULT 25
)
RETURNS SETOF public.semantic_search_result
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, extensions, pg_temp
AS $function$
DECLARE
    v_limit INTEGER;
    v_credential_id UUID;
BEGIN
    IF NOT (
        public.is_dashboard_operator()
        OR COALESCE(
             NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role',
             ''
           ) = 'service_role'
    ) THEN
        RAISE EXCEPTION 'Operator authorization required' USING ERRCODE = '42501';
    END IF;

    v_limit := LEAST(GREATEST(COALESCE(lim, 25), 1), 100);
    v_credential_id := credential_id;

    IF query_embedding IS NULL THEN
        RETURN;
    END IF;

    -- PostgreSQL drops the typmod from the function signature, so the 384-d
    -- contract is enforced here rather than by the parameter declaration.
    IF vector_dims(query_embedding) <> 384 THEN
        RAISE EXCEPTION 'query_embedding must have 384 dimensions'
            USING ERRCODE = '22000';
    END IF;

    RETURN QUERY
    SELECT
        me.message_id,
        m.credential_id,
        (me.embedding <=> query_embedding)::DOUBLE PRECISION AS distance
    FROM public.message_embeddings AS me
    JOIN public.exfiltrated_messages AS m ON m.id = me.message_id
    WHERE v_credential_id IS NULL OR m.credential_id = v_credential_id
    ORDER BY me.embedding <=> query_embedding ASC
    LIMIT v_limit;
END
$function$;

REVOKE ALL ON FUNCTION public.semantic_search(vector, uuid, integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION public.semantic_search(vector, uuid, integer) FROM anon';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.semantic_search(vector, uuid, integer) TO authenticated';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.semantic_search(vector, uuid, integer) TO service_role';
    END IF;
END $$;

COMMENT ON FUNCTION public.semantic_search(vector, uuid, integer) IS
  'Operator-or-service-role nearest-neighbour search over message_embeddings. Returns message_id, credential_id and cosine distance ordered nearest first. lim clamps to 1..100; credential_id filters when non-NULL; NULL query returns no rows.';
