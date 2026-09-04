-- Plan Item 1: Discovered credentials public view - authenticated-only access
-- Revoke anon SELECT on discovered_credentials_public
-- Grant authenticated SELECT on discovered_credentials_public
-- Keep extension INSERT/UPDATE policies on raw table (secret-gated)

-- ============================================
-- STEP 1: REVOKE ANON ACCESS TO PUBLIC VIEW
-- ============================================
-- The view exposes meta, confidence_score, collection_yield_score, chat_member_count
-- These are dashboard surfaces that should require authenticated access.
REVOKE SELECT ON public.discovered_credentials_public FROM anon;
REVOKE SELECT ON public.discovered_credentials_public FROM PUBLIC;

-- ============================================
-- STEP 2: GRANT AUTHENTICATED ACCESS
-- ============================================
-- Only authenticated operators can query credential metadata via the view.
GRANT SELECT ON public.discovered_credentials_public TO authenticated;

-- ============================================
-- VERIFICATION
-- ============================================
-- Confirm anon can no longer SELECT from the view
-- SELECT table_schema, table_name, privilege_type, grantee
-- FROM information_schema.role_table_grants
-- WHERE table_name = 'discovered_credentials_public'
--   AND table_schema = 'public'
-- ORDER BY grantee;
