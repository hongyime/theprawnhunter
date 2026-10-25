-- Migration: pgvector schema + operator-gated semantic search RPC
-- Purpose:
--   Store one local embedding per captured message and expose a nearest
--   neighbour RPC so analysts can search the corpus semantically. Embeddings
--   are produced by the heavy worker (all-MiniLM-L6-v2, 384 dimensions); this
--   migration owns only storage and retrieval.
--
-- Extension schema:
--   Supabase installs extensions in the `extensions` schema by convention, while
--   the vanilla pgvector image puts them in `public`. The session search_path is
--   widened below so unqualified `vector`, `vector_cosine_ops` and the `<=>`
--   operator resolve in either layout. CREATE EXTENSION IF NOT EXISTS keeps the
--   migration idempotent when the extension is already present.
--
-- Storage:
--   message_embeddings keys on exfiltrated_messages(id) with ON DELETE CASCADE,
--   so retention deletes never leave orphan vectors. The HNSW index uses
--   cosine distance, matching the normalisation of MiniLM embeddings.
--
-- Access:
--   Table access is service-role only (the backfill upserts with the service
--   key); authenticated clients go through public.semantic_search(), which is
--   SECURITY DEFINER and reuses public.is_dashboard_operator() exactly like
--   public.search_evidence(). No anon/authenticated table policies exist, so
--   even an accidental grant would not expose rows.
--
-- Idempotent: CREATE EXTENSION IF NOT EXISTS, CREATE TABLE IF NOT EXISTS,
-- CREATE INDEX IF NOT EXISTS, guarded CREATE TYPE, CREATE OR REPLACE FUNCTION,
-- and role-guarded REVOKE/GRANT so the file also applies on a vanilla Postgres.

CREATE EXTENSION IF NOT EXISTS vector;

-- Resolve vector types/operators in both the vanilla (public) and Supabase
-- (extensions) layouts for the statements below.
SET search_path = public, extensions, pg_temp;

CREATE TABLE IF NOT EXISTS public.message_embeddings (
    message_id UUID PRIMARY KEY
        REFERENCES public.exfiltrated_messages(id) ON DELETE CASCADE,
    embedding  vector(384) NOT NULL,
    model      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW (not IVFFlat): no training step, and recall stays stable as the corpus
-- grows. m/ef_construction are pgvector defaults tuned for ~1M vectors; cosine
-- ops match the embedding model. Re-running is a no-op.
CREATE INDEX IF NOT EXISTS idx_message_embeddings_embedding_hnsw
    ON public.message_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

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
        WHERE t.typname = 'semantic_search_result'
          AND n.nspname = 'public'
    ) THEN
        CREATE TYPE public.semantic_search_result AS (
            message_id UUID,
            credential_id UUID,
            distance DOUBLE PRECISION
        );
    END IF;
END $$;

-- Operator-gated nearest-neighbour search.
--   query_embedding : the 384-d embedded query (NULL returns no rows).
--   credential_id   : narrows to one credential only when non-NULL.
--   lim             : coerced to 1..100 (NULL -> 25).
-- Distance is cosine distance (0 = identical, 2 = opposite) and results are
-- ordered nearest first. The inner join drops embeddings whose base message no
-- longer exists, even if a future schema change weakens the cascade.
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
    IF NOT public.is_dashboard_operator() THEN
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

-- Table ACL: RLS on, no client policies, service_role only. The RPC is the only
-- authenticated surface and it never grants table privileges.
ALTER TABLE public.message_embeddings ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.message_embeddings FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON TABLE public.message_embeddings FROM anon';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON TABLE public.message_embeddings FROM authenticated';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT ALL ON TABLE public.message_embeddings TO service_role';
    END IF;
END $$;

-- Function ACL: PUBLIC and anon stay denied; authenticated may execute (the
-- function itself raises for non-operators). service_role is granted because
-- the worker/API use the service key and function privileges are checked even
-- for BYPASSRLS roles.
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

COMMENT ON TABLE public.message_embeddings IS
  'One 384-d embedding per captured message (model-scoped), keyed by exfiltrated_messages(id) with ON DELETE CASCADE. Service-role access only; retrieval is via public.semantic_search().';
COMMENT ON COLUMN public.message_embeddings.embedding IS
  'all-MiniLM-L6-v2 style embedding (384 dimensions), indexed with HNSW cosine distance.';
COMMENT ON FUNCTION public.semantic_search(vector, uuid, integer) IS
  'Operator-only nearest-neighbour search over message_embeddings. Returns message_id, credential_id and cosine distance ordered nearest first. lim clamps to 1..100; credential_id filters when non-NULL; NULL query returns no rows.';
COMMENT ON TYPE public.semantic_search_result IS
  'Result row of public.semantic_search(): message_id, credential_id, cosine distance.';
