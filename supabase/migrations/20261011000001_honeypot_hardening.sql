-- Migration: Honeypot hardening — RLS, deduplication key, and atomic redirect claims
-- Purpose:
--   1. Secure honeypot_updates table with RLS and restrict to service_role (prevent client/anon exposure).
--   2. Add update_id column with unique index on (credential_id, update_id) to prevent duplicate delivery rows.
--   3. Add redirect_claimed_at TIMESTAMPTZ column for atomic worker claims (resolving the type mismatch where 'pending' was written to redirected_at).
--
-- Idempotent — safe to re-run.

-- 1. Security: Enable RLS and restrict to service_role
ALTER TABLE public.honeypot_updates ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.honeypot_updates FROM PUBLIC;
REVOKE ALL ON public.honeypot_updates FROM anon;
REVOKE ALL ON public.honeypot_updates FROM authenticated;
GRANT ALL ON public.honeypot_updates TO service_role;

DROP POLICY IF EXISTS "Service Role Full Access" ON public.honeypot_updates;
CREATE POLICY "Service Role Full Access"
ON public.honeypot_updates
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- Ensure telegram_msg_id in exfiltrated_messages supports 64-bit integer update IDs
ALTER TABLE public.exfiltrated_messages
    ALTER COLUMN telegram_msg_id TYPE BIGINT;

-- 2. Deduplication: Add update_id column and unique index per credential
ALTER TABLE public.honeypot_updates
    ADD COLUMN IF NOT EXISTS update_id BIGINT DEFAULT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_honeypot_cred_update_id
    ON public.honeypot_updates(credential_id, update_id)
    WHERE update_id IS NOT NULL;

-- 3. Atomic claims: Add redirect_claimed_at TIMESTAMPTZ for concurrency locking
ALTER TABLE public.honeypot_updates
    ADD COLUMN IF NOT EXISTS redirect_claimed_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_honeypot_redirect_claim
    ON public.honeypot_updates(redirect_claimed_at)
    WHERE redirect_claimed_at IS NOT NULL;

COMMENT ON COLUMN public.honeypot_updates.update_id IS
    'Telegram update_id extracted from payload for deduplication on retry.';

COMMENT ON COLUMN public.honeypot_updates.redirect_claimed_at IS
    'Timestamp when a worker claimed this update for redirect dispatch; cleared on completion or failure.';
