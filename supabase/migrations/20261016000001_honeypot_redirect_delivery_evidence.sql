-- Migration: Persist Telegram redirect delivery evidence
-- Purpose: Retain the message_id returned by each successful sendMessage call
-- and the Telegram method used for the initial redirect acknowledgement.
-- Safety: Additive and idempotent. Existing rows are not backfilled.

ALTER TABLE public.honeypot_updates
    ADD COLUMN IF NOT EXISTS redirect_method TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS redirect_1_message_id BIGINT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS redirect_2_message_id BIGINT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS redirect_3_message_id BIGINT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS proactive_message_id BIGINT DEFAULT NULL;

COMMENT ON COLUMN public.honeypot_updates.redirect_method IS
    'Telegram Bot API method accepted for the initial redirect. Callback and inline acknowledgements have no message_id.';
COMMENT ON COLUMN public.honeypot_updates.redirect_1_message_id IS
    'Telegram message_id returned by the initial sendMessage redirect; NULL for callback/inline acknowledgements.';
COMMENT ON COLUMN public.honeypot_updates.redirect_2_message_id IS
    'Telegram message_id returned by the second-touch sendMessage call.';
COMMENT ON COLUMN public.honeypot_updates.redirect_3_message_id IS
    'Telegram message_id returned by the third-touch sendMessage call.';
COMMENT ON COLUMN public.honeypot_updates.proactive_message_id IS
    'Telegram message_id returned by the proactive sendMessage call.';
