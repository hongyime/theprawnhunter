-- Expose chat identity fields through the authenticated credentials view.
-- The raw table already carries chat_id/chat_name/chat_type; this migration
-- recreates the view to include them for authenticated-only UI surfaces.

CREATE OR REPLACE VIEW public.discovered_credentials_public AS
SELECT
    id,
    created_at,
    source,
    status,
    meta,
    confidence_score,
    chat_member_count,
    collection_yield_score,
    chat_id,
    chat_name,
    chat_type
FROM public.discovered_credentials;

REVOKE SELECT ON public.discovered_credentials_public FROM anon;
REVOKE SELECT ON public.discovered_credentials_public FROM PUBLIC;
GRANT SELECT ON public.discovered_credentials_public TO authenticated;

-- Backfill source chat labels for historical messages in one idempotent
-- statement. The Management API executes the whole script in a single
-- transaction, so no COMMIT is allowed.
UPDATE public.exfiltrated_messages m
SET file_meta = m.file_meta || jsonb_build_object(
    'source_chat_type',
    COALESCE(NULLIF(c.chat_type, ''), NULLIF(c.meta->>'chat_type', ''), 'unknown'),
    'source_chat_title',
    COALESCE(c.chat_name, c.meta->>'chat_name')
)
FROM public.discovered_credentials c
WHERE c.id = m.credential_id
  AND NOT (m.file_meta ? 'source_chat_type');
