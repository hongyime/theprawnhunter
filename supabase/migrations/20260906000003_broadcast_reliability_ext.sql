-- =====================================================================
-- Migration: 20260906000003_broadcast_reliability_ext.sql
-- Findings:  INTR-001 (idempotent broadcast), DATA-003 (permanent-failed classification)
-- Purpose:   Add broadcast_message_id and broadcast_status columns to
--            exfiltrated_messages so retries can detect prior success and
--            stuck rows can be classified as permanently failed instead of
--            being retried forever.
-- Safety:    Additive only. Idempotent (uses IF NOT EXISTS).
-- =====================================================================

-- Idempotency: `broadcast_message_id` is the Telegram-returned message id
-- from a successful send. When set, retries skip the send call and simply
-- flip is_broadcasted=true.
ALTER TABLE public.exfiltrated_messages
    ADD COLUMN IF NOT EXISTS broadcast_message_id BIGINT;

-- Permanent-failed classification: exfil rows that exhaust retry budget
-- transition to broadcast_status='permanent_failed' AND is_broadcasted=true
-- (so they exit the retry pool) but the new column preserves the true state.
--   'pending'          — default; not yet sent
--   'sent'             — successfully broadcast (matches is_broadcasted=true)
--   'permanent_failed' — retry budget exhausted; will not be attempted again
--   'revoked'          — parent credential is revoked; not broadcastable
ALTER TABLE public.exfiltrated_messages
    ADD COLUMN IF NOT EXISTS broadcast_status TEXT
        CHECK (broadcast_status IN ('pending', 'sent', 'permanent_failed', 'revoked'));

-- Backfill existing rows so the column is non-null-consistent:
--   is_broadcasted=true  → 'sent'
--   is_broadcasted=false → 'pending' (retry loop keeps them here)
UPDATE public.exfiltrated_messages
   SET broadcast_status = CASE
           WHEN is_broadcasted IS TRUE THEN 'sent'
           ELSE 'pending'
       END
 WHERE broadcast_status IS NULL;

-- Partial index for permanent-failed drill-down.
CREATE INDEX IF NOT EXISTS idx_messages_permanent_failed
    ON public.exfiltrated_messages (created_at DESC)
    WHERE broadcast_status = 'permanent_failed';

COMMENT ON COLUMN public.exfiltrated_messages.broadcast_message_id IS
    'Telegram message_id returned by successful sendMessage. When non-NULL, retry logic skips send and marks is_broadcasted=true. See INTR-001.';

COMMENT ON COLUMN public.exfiltrated_messages.broadcast_status IS
    'Explicit broadcast state: pending, sent, permanent_failed, revoked. is_broadcasted alone cannot distinguish successful send from give-up. See DATA-003.';

-- =====================================================================
-- ROLLBACK (paste manually if reverting)
-- =====================================================================
-- DROP INDEX IF EXISTS public.idx_messages_permanent_failed;
-- ALTER TABLE public.exfiltrated_messages DROP COLUMN IF EXISTS broadcast_status;
-- ALTER TABLE public.exfiltrated_messages DROP COLUMN IF EXISTS broadcast_message_id;
