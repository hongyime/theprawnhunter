-- Plan Item 1: Evidence Surface Hardening (VALID PostgreSQL)
-- Revoke ALL raw access from anon, authenticated, and PUBLIC.
-- Authenticated operators must use evidence_redacted view.
-- Service role retains full bypass access.

-- ============================================
-- STEP 1: DROP POLICIES THAT ALLOW RAW ACCESS
-- ============================================
DROP POLICY IF EXISTS "Authenticated Read Access" ON public.exfiltrated_messages;
DROP POLICY IF EXISTS "Anon Read Access" ON public.exfiltrated_messages;

-- ============================================
-- STEP 2: REVOKE ALL RAW TABLE ACCESS
-- ============================================
-- Revoke from PUBLIC role (catches any implicit grants)
REVOKE ALL ON public.exfiltrated_messages FROM PUBLIC;
-- Revoke from anon and authenticated explicitly
REVOKE ALL ON public.exfiltrated_messages FROM anon;
REVOKE ALL ON public.exfiltrated_messages FROM authenticated;

-- ============================================
-- STEP 3: SERVICE ROLE POLICY (safe, idempotent)
-- ============================================
DROP POLICY IF EXISTS "Service Role Full Access" ON public.exfiltrated_messages;

CREATE POLICY "Service Role Full Access"
ON public.exfiltrated_messages
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- ============================================
-- STEP 4: RECREATE EVIDENCE_REDACTED VIEW
-- ============================================
DROP VIEW IF EXISTS public.evidence_redacted;

CREATE VIEW public.evidence_redacted
WITH (security_invoker = false) AS
SELECT
    id,
    credential_id,
    -- Irreversible pseudonym using md5 (portable, no pgcrypto required)
    'user_' || substring(md5(sender_name || 'salt_pr4wn_hunt3r'), 1, 8) AS sender_pseudonym,
    -- Mask token patterns BEFORE truncation to prevent boundary leaks
    regexp_replace(
        left(
            regexp_replace(
                COALESCE(content, ''),
                '\d{8,10}:[A-Za-z0-9_-]{30,}',
                '[TOKEN]',
                'g'
            ),
            500
        ),
        '\d{8,10}:[A-Za-z0-9_-]{30,}',
        '[TOKEN]',
        'g'
    ) AS content,
    media_type,
    is_broadcasted,
    created_at
FROM public.exfiltrated_messages;

-- ============================================
-- STEP 5: GRANT REDACTED VIEW TO AUTHENTICATED ONLY
-- ============================================
-- Revoke from PUBLIC and anon (belt-and-suspenders)
REVOKE ALL ON public.evidence_redacted FROM PUBLIC;
REVOKE ALL ON public.evidence_redacted FROM anon;

-- Grant to authenticated operators only
GRANT SELECT ON public.evidence_redacted TO authenticated;

-- Comment documenting the view
COMMENT ON VIEW public.evidence_redacted IS
    'Redacted evidence view for authenticated operators: content token-masked then truncated to 500 chars, sender replaced with irreversible md5 pseudonym, telegram_msg_id omitted, file_meta and broadcast_error omitted. AUTHENTICATED-ONLY. See supabase/migrations/20260903000004_rls_hardening.sql.';

-- ============================================
-- VERIFICATION QUERIES
-- ============================================
-- Confirm no raw policies for anon/authenticated on exfiltrated_messages
SELECT 'Should return 0 rows' AS check,
       COUNT(*) AS raw_access_policies
FROM pg_policies
WHERE tablename = 'exfiltrated_messages'
  AND schemaname = 'public'
  AND roles::text !~ 'service_role';

-- Confirm evidence_redacted grants (authenticated only)
SELECT table_schema, table_name, privilege_type, grantee
FROM information_schema.role_table_grants
WHERE table_name = 'evidence_redacted'
  AND table_schema = 'public'
ORDER BY grantee;
