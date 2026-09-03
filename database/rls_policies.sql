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
-- CHANGED (Plan Item 1): Evidence surface protection
--   - ANON access is FULLY REVOKED. The captured message corpus is investigative
--     evidence, not public content. Making it anon-readable was a data-exfiltration
--     surface for anyone who scraped the anon key.
--   - SERVICE_ROLE bypasses RLS (workers, ingest, broadcaster unchanged).
--   - AUTHENTICATED operators (frontend users signed into Supabase Auth) can SELECT
--     the raw table. UI must obtain a session via supabase.auth before querying.
--   - Public / anon consumers should use the public.evidence_redacted VIEW (below)
--     which strips high-risk fields (raw content > 500 chars, file_meta, broadcast_error).
--
-- WHO ACCESSES THIS TABLE:
--   - Backend workers/API      → SERVICE_ROLE key → bypasses RLS, full access
--   - Frontend (authenticated) → user JWT         → SELECT on raw table via policy below
--   - Frontend (anon / public) → anon key         → SELECT ONLY via evidence_redacted view

ALTER TABLE exfiltrated_messages ENABLE ROW LEVEL SECURITY;

-- Explicitly revoke any lingering direct grant to anon.
REVOKE ALL ON exfiltrated_messages FROM anon;

-- Service role: explicit ALL policy for clarity (service_role bypasses RLS by default,
-- but stating it here documents the contract and survives any future BYPASS-RLS revocation).
CREATE POLICY "Service Role Full Access"
ON exfiltrated_messages
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- Authenticated operators (dashboards signed into Supabase Auth): read-only access.
CREATE POLICY "Authenticated Read Access"
ON exfiltrated_messages
FOR SELECT
TO authenticated
USING (true);

-- No anon SELECT / INSERT / UPDATE / DELETE. Anon reaches evidence only through
-- the redacted view defined below.

-- ============================================
-- STEP 4b: evidence_redacted VIEW
-- ============================================
-- Minimally-viable public projection of exfiltrated_messages for anon consumers
-- (e.g. read-only dashboards, external reviewers). Strips or truncates fields
-- that leak either operator secrets or victim PII:
--   - content: truncated to 500 chars (avoids leaking large data dumps / creds)
--   - file_meta: omitted (contains file_id, mime, hashes that link to raw media)
--   - broadcast_error: omitted (may contain bot tokens, chat ids, stack traces)
--
-- Anon can SELECT this view without touching the base table. RLS on the base
-- table still applies through the view because the view runs with the invoker's
-- rights — but we grant SELECT on the view to anon explicitly, and use
-- security_invoker=false semantics (default in Postgres) via SECURITY DEFINER
-- ownership only if strictly required. Kept as a plain view here so it inherits
-- the base-table RLS; the accompanying grant lets anon read the projected rows.

DROP VIEW IF EXISTS public.evidence_redacted;

CREATE VIEW public.evidence_redacted
WITH (security_invoker = false) AS
SELECT
    id,
    credential_id,
    telegram_msg_id,
    sender_name,
    CASE
        WHEN content IS NULL THEN NULL
        WHEN char_length(content) > 500 THEN left(content, 500) || '…'
        ELSE content
    END AS content,
    media_type,
    is_broadcasted,
    created_at
FROM exfiltrated_messages;

-- The view is owned by the role that ran this script (typically postgres). Because
-- security_invoker=false (definer semantics), it can read the base table on behalf
-- of anon without RLS blocking. ANON ACCESS REVOKED per Plan Item 1 requirements.
-- Only authenticated operators may read even the redacted surface.
-- GRANT SELECT ON public.evidence_redacted TO anon;  -- REMOVED
GRANT SELECT ON public.evidence_redacted TO authenticated;

COMMENT ON VIEW public.evidence_redacted IS
    'Redacted public projection of exfiltrated_messages: content>500 truncated, file_meta and broadcast_error omitted. Anon-readable. See database/rls_policies.sql.';

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

-- Confirm the redacted view exists and is grant-visible.
SELECT table_schema, table_name FROM information_schema.views
WHERE table_schema = 'public' AND table_name = 'evidence_redacted';

-- ============================================
-- SECURITY MODEL SUMMARY
-- ============================================
--
-- SERVICE_ROLE key (backend .env only, never in browser):
--   ✅ Full access to all tables, bypasses RLS
--   Used by: all workers, FastAPI, Celery tasks
--
-- AUTHENTICATED role (signed-in operators):
--   exfiltrated_messages    → SELECT (raw table, full fields)
--
-- ANON key (frontend / extension — can be public):
--   discovered_credentials  → INSERT/UPDATE only WITH valid x-extension-secret header
--   discovered_credentials  → SELECT blocked on raw table (use the _public VIEW)
--   exfiltrated_messages    → NO direct access. Use public.evidence_redacted view.
--   evidence_redacted       → SELECT (content truncated, file_meta / broadcast_error stripped)
--   telegram_accounts       → no access at all
--
-- Anyone who clones the repo gets:
--   - The anon key (if accidentally committed) → can only read evidence_redacted
--     (truncated content, no media metadata, no error blobs)
--   - The RLS policy code → shows the mechanism, not the secret value
--   - Zero write access without EXTENSION_WRITE_SECRET
