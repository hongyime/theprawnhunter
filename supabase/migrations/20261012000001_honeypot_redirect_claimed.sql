-- Add missing redirect_claimed_at column to honeypot_updates
ALTER TABLE honeypot_updates ADD COLUMN IF NOT EXISTS redirect_claimed_at TIMESTAMPTZ;
