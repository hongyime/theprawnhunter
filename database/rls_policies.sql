-- ============================================
-- Row Level Security (RLS) Policies
-- Execute these in Supabase SQL Editor
-- ============================================

-- ============================================
-- STEP 1: DROP ALL EXISTING POLICIES
-- ============================================
DROP POLICY IF EXISTS "Backend Only Access"       ON discovered_credentials;
DROP POLICY IF EXISTS "Deny All Public Access"    ON discovered_credentials;
DROP POLICY IF EXISTS "Allow Backend Writes"      ON discovered_credentials;
DROP POLICY IF EXISTS "Deny Public Reads"         ON discovered_credentials;
DROP POLICY IF EXISTS "Allow Backend Insert"      ON discovered_credentials;
DROP POLICY IF EXISTS "Allow Backend Update"      ON discovered_credentials;
DROP POLICY IF EXISTS "Allow Backend Delete"      ON discovered_credentials;
DROP POLICY IF EXISTS "Allow Public Reads"        ON discovered_credentials;
DROP POLICY IF EXISTS "Extension Insert"          ON discovered_credentials;
DROP POLICY IF EXISTS "Extension Update"          ON discovered_credentials;

DROP POLICY IF EXISTS "Public Read Access"        ON exfiltrated_messages;
DROP POLICY IF EXISTS "Deny Public Modifications" ON exfiltrated_messages;
DROP POLICY IF EXISTS "Deny Public Updates"       ON exfiltrated_messages;
DROP POLICY IF EXISTS "Deny Public Deletes"       ON exfiltrated_messages;
DROP POLICY IF EXISTS "Allow Backend Writes"      ON exfiltrated_messages;
DROP POLICY IF EXISTS "Allow Backend Insert"      ON exfiltrated_messages;
DROP POLICY IF EXISTS "Allow Backend Update"      ON exfiltrated_messages;
DROP POLICY IF EXISTS "Allow Backend Delete"      ON exfiltrated_messages;
DROP POLICY IF EXISTS "Anon Read Access"          ON exfiltrated_messages;
DROP POLICY IF EXISTS "Service Role Full Access"  ON exfiltrated_messages;
DROP POLICY IF EXISTS "Authenticated Read Access" ON exfiltrated_messages;

-- ============================================
-- STEP 2: STORE THE EXTENSION WRITE SECRET
-- ============================================
-- Run this ONCE, replacing the placeholder with your EXTENSION_WRITE_SECRET from .env.
-- This value lives only inside your Supabase database — never in source control.
--
--   ALTER DATABASE postgres
--     SET app.extension_write_secret = 'your_EXTENSION_WRITE_SECRET_value_here';
--   SELECT pg_reload_conf();
--
-- Retrieve it anytime:
--   SELECT current_setting('app.extension_write_secret');

-- ============================================
-- STEP 3: discovered_credentials TABLE
-- ============================================
--
-- WHO ACCESSES THIS TABLE:
--   - Backend workers/API  → SERVICE_ROLE key → bypasses RLS entirely, no policy needed
--   - Chrome extension     → anon key         → INSERT/UPDATE only, gated by write secret
--   - Frontend (Sidebar)   → anon key         → SELECT via discovered_credentials_public VIEW only
--                                                (the VIEW is defined in init.sql)
--
-- RESULT: anon can never SELECT the raw table (bot_token, token_hash etc. stay hidden).
--         anon can INSERT/UPDATE only when the correct write secret header is present.
--         Service role bypasses everything — workers are unaffected.

ALTER TABLE discovered_credentials ENABLE ROW LEVEL SECURITY;

-- Extension INSERT: only when x-extension-secret header matches the DB-stored secret
CREATE POLICY "Extension Insert"
ON discovered_credentials
FOR INSERT
TO anon
WITH CHECK (
    (current_setting('request.headers', true)::json ->> 'x-extension-secret')
        = current_setting('app.extension_write_secret', true)
    AND current_setting('app.extension_write_secret', true) IS NOT NULL
    AND current_setting('app.extension_write_secret', true) <> ''
);

-- Extension UPDATE: same secret check
CREATE POLICY "Extension Update"
ON discovered_credentials
FOR UPDATE
TO anon
USING (
    (current_setting('request.headers', true)::json ->> 'x-extension-secret')
        = current_setting('app.extension_write_secret', true)
    AND current_setting('app.extension_write_secret', true) IS NOT NULL
    AND current_setting('app.extension_write_secret', true) <> ''
)
WITH CHECK (
    (current_setting('request.headers', true)::json ->> 'x-extension-secret')
        = current_setting('app.extension_write_secret', true)
    AND current_setting('app.extension_write_secret', true) IS NOT NULL
    AND current_setting('app.extension_write_secret', true) <> ''
);

-- No anon SELECT on the raw table — frontend must use the discovered_credentials_public VIEW.
-- No anon DELETE — only service role can delete.

-- ============================================
-- STEP 4: exfiltrated_messages TABLE  (HARDENED)
-- ============================================
--
-- SECURITY MODEL: Evidence surface protection (Plan Item 1)
--   - RAW ACCESS: service_role ONLY (bypasses RLS, workers unchanged)
--   - ANON access: FULLY REVOKED (no raw table, no view)
--   - AUTHENTICATED access: FULLY REVOKED on raw table
--   - REDACTED ACCESS: authenticated operators use evidence_redacted view
--
-- RATIONALE:
--   - Captured messages are investigative evidence, not public content.
--   - Raw content, sender identity, and message IDs can leak tokens/secrets.
--   - Authenticated operators must use the redacted view for safety.
--
-- WHO ACCESSES THIS TABLE:
--   - Backend workers/API      → SERVICE_ROLE key → bypasses RLS, full access
--   - Frontend (authenticated) → user JWT         → NO raw access, use evidence_redacted
--   - Frontend (anon / public) → anon key         → NO access at all

ALTER TABLE public.exfiltrated_messages ENABLE ROW LEVEL SECURITY;

-- Revoke ALL raw access from PUBLIC, anon, and authenticated
REVOKE ALL ON public.exfiltrated_messages FROM PUBLIC;
REVOKE ALL ON public.exfiltrated_messages FROM anon;
REVOKE ALL ON public.exfiltrated_messages FROM authenticated;

-- Service role: explicit ALL policy for clarity (documents the contract)
DROP POLICY IF EXISTS "Service Role Full Access" ON public.exfiltrated_messages;
CREATE POLICY "Service Role Full Access"
ON public.exfiltrated_messages
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- NO POLICIES for anon or authenticated - they must use evidence_redacted

-- ============================================
-- STEP 4b: evidence_redacted VIEW
-- ============================================
-- Authenticated-only redacted projection for safe operator review.
-- Strips/truncates fields that leak secrets or PII:
--   - content: token patterns masked BEFORE truncation to 500 chars
--   - sender_name: replaced with irreversible hash-based pseudonym
--   - telegram_msg_id: omitted entirely
--   - file_meta: omitted (media file IDs, hashes)
--   - broadcast_error: omitted (bot tokens, chat IDs, stack traces)
--
-- Authenticated operators MUST query this view, not the raw table.

DROP VIEW IF EXISTS public.evidence_redacted;

CREATE VIEW public.evidence_redacted
WITH (security_invoker = false) AS
SELECT
    id,
    credential_id,
    'user_' || substring(sha256((sender_name || 'salt_pr4wn_hunt3r')::bytea)::text, 1, 8) AS sender_pseudonym,
    regexp_replace(
        left(
            regexp_replace(content, '\d{8,10}:[A-Za-z0-9_-]{30,}', '[TOKEN]', 'g'),
            500
        ),
        '\d{8,10}:[A-Za-z0-9_-]{30,}',
        '[TOKEN]',
        'g'
    ) AS content,
    media_type,
    is_broadcasted,
    created_at
FROM public.exfiltrated_messages
WHERE content IS NOT NULL;

-- Revoke from PUBLIC and anon
REVOKE ALL ON public.evidence_redacted FROM PUBLIC;
REVOKE ALL ON public.evidence_redacted FROM anon;

-- Authenticated-only access to the redacted view
GRANT SELECT ON public.evidence_redacted TO authenticated;

COMMENT ON VIEW public.evidence_redacted IS
    'Redacted evidence view for authenticated operators: content token-masked then truncated to 500 chars, sender replaced with irreversible pseudonym, telegram_msg_id omitted, file_meta and broadcast_error omitted. AUTHENTICATED-ONLY. See database/rls_policies.sql and supabase/migrations/20260903000004_rls_hardening.sql.';

-- ============================================
-- STEP 5: telegram_accounts TABLE
-- ============================================
--
-- WHO ACCESSES THIS TABLE:
--   - bot_listener.py → SERVICE_ROLE key → bypasses RLS, no policy needed
--   - Nobody else
--
-- RESULT: anon has zero access. No policies needed — RLS enabled = deny by default.

ALTER TABLE telegram_accounts ENABLE ROW LEVEL SECURITY;

-- No policies for anon. Service role bypasses RLS and handles all access.

-- ============================================
-- VERIFICATION QUERIES
-- ============================================
SELECT schemaname, tablename, rowsecurity
FROM pg_tables
WHERE tablename IN ('discovered_credentials', 'exfiltrated_messages', 'telegram_accounts');

SELECT schemaname, tablename, policyname, permissive, roles, cmd
FROM pg_policies
WHERE tablename IN ('discovered_credentials', 'exfiltrated_messages', 'telegram_accounts')
ORDER BY tablename, policyname;
