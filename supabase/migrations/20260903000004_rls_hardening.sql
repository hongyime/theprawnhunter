-- Plan Item 1: Evidence Surface Hardening
-- Revoke ALL raw access from both anon AND authenticated.
-- Authenticated operators must use evidence_redacted view.
-- Service role retains full bypass access.

-- ============================================
-- STEP 1: DROP POLICIES THAT ALLOW RAW ACCESS
-- ============================================
DROP POLICY IF EXISTS "Authenticated Read Access" ON exfiltrated_messages;
DROP POLICY IF EXISTS "Anon Read Access" ON exfiltrated_messages;

-- ============================================
-- STEP 2: REVOKE ALL RAW TABLE ACCESS
-- ============================================
REVOKE ALL ON exfiltrated_messages FROM anon;
REVOKE ALL ON exfiltrated_messages FROM authenticated;

-- ============================================
-- STEP 3: KEEP SERVICE ROLE FULL ACCESS
-- ============================================
-- Service role bypasses RLS by default; this policy documents the contract.
CREATE POLICY IF NOT EXISTS "Service Role Full Access"
ON exfiltrated_messages
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
    telegram_msg_id,
    -- Pseudonymize sender: show only first 3 chars + '...'
    CASE
        WHEN sender_name IS NULL THEN NULL
        WHEN char_length(sender_name) > 3 THEN left(sender_name, 3) || '...'
        ELSE sender_name
    END AS sender_name,
    -- Truncate content to 500 chars AND mask token-like patterns
    CASE
        WHEN content IS NULL THEN NULL
        WHEN char_length(content) > 500 THEN
            regexp_replace(
                left(content, 500) || '…',
                '\d{8,10}:[A-Za-z0-9_-]{30,}', '[TOKEN]',
                'g'
            )
        ELSE
            regexp_replace(
                content,
                '\d{8,10}:[A-Za-z0-9_-]{30,}', '[TOKEN]',
                'g'
            )
    END AS content,
    media_type,
    is_broadcasted,
    created_at
FROM exfiltrated_messages;

-- ============================================
-- STEP 5: GRANT REDACTED VIEW TO AUTHENTICATED ONLY
-- ============================================
-- REVOKE any anon grant (belt-and-suspenders)
REVOKE ALL ON public.evidence_redacted FROM anon;

-- GRANT to authenticated operators only
GRANT SELECT ON public.evidence_redacted TO authenticated;

COMMENT ON VIEW public.evidence_redacted IS
    'Redacted evidence view for authenticated operators: content truncated to 500 chars, tokens masked, sender pseudonymized, file_meta and broadcast_error omitted. Authenticated-only. See supabase/migrations/20260903000004_rls_hardening.sql.';

-- ============================================
-- VERIFICATION QUERIES
-- ============================================
-- Confirm no raw policies for anon/authenticated on exfiltrated_messages
SELECT 'exfiltrated_messages policies should be service_role only' AS check_type,
       COUNT(*) AS policy_count
FROM pg_policies
WHERE tablename = 'exfiltrated_messages'
  AND policyname NOT LIKE 'Service Role%';

-- Should return 0 policies

-- Confirm evidence_redacted grants
SELECT table_schema, table_name, privilege_type, grantee
FROM information_schema.role_table_grants
WHERE table_name = 'evidence_redacted'
ORDER BY grantee;
