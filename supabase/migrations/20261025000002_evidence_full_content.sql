-- Migration: expose full evidence content to authorized dashboard operators
-- Purpose:
--   Analysts triaging a takeover need the complete captured message. The
--   previous projection truncated every message to its first 500 characters
--   and replaced bot-token shapes with a placeholder, which hid the context
--   that makes a hit actionable. This migration recreates
--   public.evidence_redacted with the raw content column.
--
-- What is removed:
--   - the left(...,500) truncation, and
--   - the bot-token regexp substitution.
--   No other object applies either transform; the raw table itself was never
--   truncated.
--
-- What is added:
--   sender_key, a stable per-sender key. It derives from the immutable numeric
--   sender_user_id (added in 20260805000002_sender_user_id.sql) and falls back
--   to the display name only when the id is NULL. The salt matches
--   sender_pseudonym so both identity columns agree. sender_name alone is not
--   a reliable key: display names change and are not unique. If this migration
--   is ever replayed against a database without the sender_user_id column, the
--   same COALESCE degrades to the sender_name fallback.
--
-- What is unchanged:
--   - the public.is_dashboard_operator() authorization predicate on rows,
--   - security_invoker = false (the view is owned by the migration role and
--     reads the raw table without granting it to clients),
--   - security_barrier = true (caller predicates cannot be pushed below the
--     authorization filter),
--   - raw-table grants stay revoked; the view is the only evidence surface.
--
-- Idempotent: the view is dropped and recreated so the column list can change,
-- then its ACLs are re-applied. Role statements are guarded so the migration
-- also applies on a vanilla Postgres without Supabase's roles.

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
    'user_'::text || "substring"(md5(COALESCE(sender_user_id::text, sender_name, '') || 'salt_pr4wn_hunt3r'::text), 1, 8) AS sender_key
  FROM public.exfiltrated_messages
 WHERE public.is_dashboard_operator();

-- The view no longer sanitizes text, so the operator predicate is the only
-- control. Drop the inherited ACL (drop+create already does) and rebuild it:
-- PUBLIC and anon stay denied, authenticated may SELECT but sees zero rows
-- unless is_dashboard_operator() is true.
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
  'Read-only full-content evidence for authorized operators. Rows are filtered by public.is_dashboard_operator(); the definer view is a security barrier and raw-table grants remain revoked. content is not truncated or token-masked; sender_key is a salted md5 of sender_user_id (sender_name fallback).';
