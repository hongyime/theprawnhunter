-- Migration: expose a whitelisted file_meta projection to dashboard operators
-- Purpose:
--   The operator evidence feed omitted file_meta entirely, so the dashboard
--   could never render thumbnails, ASR transcripts or OCR text from live rows,
--   and semantic hydration had no media text to build snippets from.
--
-- What is added:
--   one new jsonb column, file_meta, that is NOT the raw column. It is a
--   projection containing exactly three keys, each copied only when present:
--     - thumb_path      (private bucket object key written by thumbnails.py)
--     - transcript_text (written by heavy.transcribe_media)
--     - ocr_text        (written by heavy.ocr_media)
--   jsonb_build_object + jsonb_strip_nulls means absent keys disappear instead
--   of appearing as null. File ids, access hashes, mime blobs and every other
--   storage key stay behind the view boundary.
--
-- What is unchanged:
--   - every column of 20261025000002_evidence_full_content.sql, in the same
--     order, before the new column,
--   - public.is_dashboard_operator() row authorization,
--   - security_invoker = false (the view owner reads the raw table; clients
--     never receive a base-table grant),
--   - security_barrier = true (caller predicates cannot be pushed below the
--     authorization filter),
--   - raw-table grants stay revoked.
--
-- Idempotent: the view is dropped and recreated because the column list
-- changes, then the ACLs from the previous definition are re-applied. Role
-- statements are guarded so the migration also applies on vanilla Postgres
-- without Supabase's roles.

DROP VIEW IF EXISTS public.evidence_redacted;

CREATE VIEW public.evidence_redacted
WITH (security_invoker = false, security_barrier = true) AS
SELECT
    id,
    credential_id,
    'user_'::text || "substring"(md5(sender_name || 'salt_pr4wn_hunt3r'::text), 1, 8) AS sender_pseudonym,
    content,
    media_type,
    is_broadcasted,
    created_at,
    'user_'::text || "substring"(md5(COALESCE(sender_user_id::text, sender_name, '') || 'salt_pr4wn_hunt3r'::text), 1, 8) AS sender_key,
    CASE
        WHEN file_meta IS NULL THEN NULL
        ELSE jsonb_strip_nulls(jsonb_build_object(
            'thumb_path', file_meta->'thumb_path',
            'transcript_text', file_meta->'transcript_text',
            'ocr_text', file_meta->'ocr_text'
        ))
    END AS file_meta
  FROM public.exfiltrated_messages
 WHERE public.is_dashboard_operator();

-- Drop the inherited ACL (drop+create already does) and rebuild it exactly as
-- before: PUBLIC and anon stay denied, authenticated may SELECT but sees zero
-- rows unless is_dashboard_operator() is true.
REVOKE ALL ON public.evidence_redacted FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON public.evidence_redacted FROM anon';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON public.evidence_redacted FROM authenticated';
        EXECUTE 'GRANT SELECT ON public.evidence_redacted TO authenticated';
    END IF;
END $$;

COMMENT ON VIEW public.evidence_redacted IS
  'Read-only full-content evidence for authorized operators. Rows are filtered by public.is_dashboard_operator(); the definer view is a security barrier and raw-table grants remain revoked. content is not truncated or token-masked; sender_key is a salted md5 of sender_user_id (sender_name fallback). file_meta is a whitelisted projection of thumb_path, transcript_text and ocr_text only.';
