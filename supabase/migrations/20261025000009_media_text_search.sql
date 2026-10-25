-- Migration: index transcript/OCR text as searchable evidence
-- Purpose:
--   Migration 20261025000003 built search_vector from caption `content` only.
--   ASR (heavy.transcribe_media) writes file_meta.transcript_text and OCR
--   (heavy.ocr_media) writes file_meta.ocr_text, so spoken/printed words were
--   invisible to search_evidence(). This migration extends the existing vector
--   (no new column, no second RPC) and the snippet source to the full derived
--   document.
--
-- Text expression (single source of truth for this migration):
--   coalesce(content, '') || ' ' ||
--   coalesce(file_meta->>'transcript_text', '') || ' ' ||
--   coalesce(file_meta->>'ocr_text', '')
--
--   app/services/media_text.py:searchable_media_text() builds the same logical
--   document in Python (content, transcript, OCR joined by newlines). Newline
--   and space are both whitespace separators for to_tsvector('simple', ...),
--   so the indexed lexemes match the text embedded by the incremental hook and
--   scripts/ops/backfill_embeddings.py. Keep both expressions in sync.
--
-- Trigger:
--   BEFORE INSERT OR UPDATE OF content, file_meta. Adding file_meta to the
--   update columns means an ASR/OCR write re-indexes the row automatically.
--   Backfill UPDATEs set only search_vector, so they do not re-fire the
--   trigger.
--
-- Backfill:
--   5,000-row batches, only where the stored vector differs from the derived
--   expression (search_vector IS NULL or stale content-only/media text). This
--   terminates and makes a second apply a true no-op; the comparison is the
--   "derived text changed" predicate.
--   Caveat (same as 000003): a DO block is one transaction, so row locks are
--   held until commit. Batching bounds per-statement memory/WAL, it is not an
--   online rewrite. Schedule large applies in a quiet window.
--
-- Index:
--   The GIN index from 000003 already covers the same search_vector column;
--   the stored values change but the index definition does not, so no rebuild
--   is required. New/updated rows flow through the trigger.
--
-- Idempotent: CREATE OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS, value-based
-- backfill (0 rows on re-apply), CREATE OR REPLACE search_evidence with the
-- same signature/return type, role-guarded ACL statements. Role statements run
-- on a vanilla Postgres without Supabase's roles too.

CREATE OR REPLACE FUNCTION public.evidence_search_vector_update()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    NEW.search_vector := to_tsvector(
        'simple',
        COALESCE(NEW.content, '') || ' ' ||
        COALESCE(NEW.file_meta->>'transcript_text', '') || ' ' ||
        COALESCE(NEW.file_meta->>'ocr_text', '')
    );
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS evidence_search_vector_update
    ON public.exfiltrated_messages;
CREATE TRIGGER evidence_search_vector_update
BEFORE INSERT OR UPDATE OF content, file_meta ON public.exfiltrated_messages
FOR EACH ROW EXECUTE FUNCTION public.evidence_search_vector_update();

DO $$
DECLARE
    batch_size CONSTANT INTEGER := 5000;
    rows_updated INTEGER;
BEGIN
    LOOP
        UPDATE public.exfiltrated_messages AS m
        SET search_vector = to_tsvector(
            'simple',
            COALESCE(m.content, '') || ' ' ||
            COALESCE(m.file_meta->>'transcript_text', '') || ' ' ||
            COALESCE(m.file_meta->>'ocr_text', '')
        )
        WHERE m.id IN (
            SELECT pending.id
            FROM public.exfiltrated_messages AS pending
            WHERE pending.search_vector IS DISTINCT FROM to_tsvector(
                'simple',
                COALESCE(pending.content, '') || ' ' ||
                COALESCE(pending.file_meta->>'transcript_text', '') || ' ' ||
                COALESCE(pending.file_meta->>'ocr_text', '')
            )
            ORDER BY pending.id
            LIMIT batch_size
        );

        GET DIAGNOSTICS rows_updated = ROW_COUNT;
        EXIT WHEN rows_updated = 0;

        RAISE NOTICE 'media_text_search backfill: % rows', rows_updated;
    END LOOP;
END $$;

-- Same signature, return type, operator gate and result columns as 000003.
-- Only the ts_headline source changed: snippets now window the derived
-- document so an ASR/OCR keyword is highlighted in place.
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
  'Operator-only full-text evidence search over content + file_meta transcript/OCR. Returns highlighted snippets from public.exfiltrated_messages, newest first. lim clamps to 1..200; credential_id filters when non-NULL; malformed query text falls back instead of raising.';
COMMENT ON FUNCTION public.evidence_search_vector_update() IS
  'BEFORE INSERT OR UPDATE OF content, file_meta trigger helper: maintains exfiltrated_messages.search_vector from content plus file_meta.transcript_text and file_meta.ocr_text.';
COMMENT ON COLUMN public.exfiltrated_messages.search_vector IS
  'Full-text vector for evidence search (content + transcript + OCR), maintained by evidence_search_vector_update() and searched by public.search_evidence().';
