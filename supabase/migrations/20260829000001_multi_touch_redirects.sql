-- Migration: multi-touch redirect reminders + proactive outreach
-- Purpose: 
--   1. Track 3-tier redirect message sequence (immediate, 24h, 72h)
--   2. Track proactive outreach sent status
--   3. Enable more update types for redirect capture
--
-- Idempotent — safe to re-run.

-- Multi-touch redirect tracking
ALTER TABLE public.honeypot_updates
    ADD COLUMN IF NOT EXISTS redirect_attempt INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS redirect_1_sent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS redirect_2_sent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS redirect_3_sent_at TIMESTAMPTZ;

-- Proactive outreach tracking
ALTER TABLE public.honeypot_updates
    ADD COLUMN IF NOT EXISTS proactive_sent_at TIMESTAMPTZ;

-- Index for finding users needing redirect attempt 2 (sent 1, 24+ hours ago, no reply)
CREATE INDEX IF NOT EXISTS idx_honeypot_redirect_attempt_2
    ON public.honeypot_updates(credential_id, redirect_1_sent_at)
    WHERE redirect_attempt = 1 
      AND redirected_at IS NULL 
      AND redirect_2_sent_at IS NULL;

-- Index for finding users needing redirect attempt 3 (sent 2, 48+ hours ago, no reply)
CREATE INDEX IF NOT EXISTS idx_honeypot_redirect_attempt_3
    ON public.honeypot_updates(credential_id, redirect_2_sent_at)
    WHERE redirect_attempt = 2 
      AND redirected_at IS NULL 
      AND redirect_3_sent_at IS NULL;

-- Index for proactive outreach (users not yet contacted)
CREATE INDEX IF NOT EXISTS idx_honeypot_proactive_pending
    ON public.honeypot_updates(credential_id, sender_user_id)
    WHERE proactive_sent_at IS NULL
      AND sender_user_id IS NOT NULL;

-- Drop old index that only indexed 'message' type
DROP INDEX IF EXISTS idx_honeypot_unredir;

-- New index covers all relevant update types
CREATE INDEX IF NOT EXISTS idx_honeypot_unredir_all_types
    ON public.honeypot_updates(update_type, received_at)
    WHERE redirected_at IS NULL 
      AND update_type IN ('message', 'callback_query', 'inline_query', 'edited_message', 'channel_post');

-- Index for callback_query hijack (find users who clicked buttons)
CREATE INDEX IF NOT EXISTS idx_honeypot_callback_pending
    ON public.honeypot_updates(received_at)
    WHERE redirected_at IS NULL 
      AND update_type = 'callback_query';

-- Index for inline_query hijack (find users who searched)
CREATE INDEX IF NOT EXISTS idx_honeypot_inline_pending
    ON public.honeypot_updates(received_at)
    WHERE redirected_at IS NULL 
      AND update_type = 'inline_query';

-- Comment documenting the redirect attempt meanings
COMMENT ON COLUMN public.honeypot_updates.redirect_attempt IS 
'0 = no redirect sent yet, 1 = message 1 sent, 2 = message 2 sent, 3 = final notice sent';
