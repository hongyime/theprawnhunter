-- =====================================================================
-- 2026-09-06 REMEDIATION — combined migration blob for Supabase SQL editor.
--
-- Paste this ENTIRE file into the Supabase SQL editor and run.
-- Order is: verify → broadcast reliability → media hashes → audit logs idx.
-- All statements are idempotent (IF NOT EXISTS / DO $$ guards).
-- If verify (block 1) raises, apply the pending analyst-workflow migrations
-- listed there first, then re-run this file.
-- =====================================================================

-- =====================================================================
-- BLOCK 1: verify pending migrations (from 20260906000006)
-- =====================================================================

DO $$
DECLARE
    expected_tables text[] := ARRAY['findings','finding_evidence','engagement_events','honeypot_redirect_log'];
    missing text[] := ARRAY[]::text[];
    t text;
BEGIN
    FOREACH t IN ARRAY expected_tables LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename = t
        ) THEN
            missing := array_append(missing, t);
        END IF;
    END LOOP;

    IF array_length(missing, 1) > 0 THEN
        RAISE EXCEPTION 'DATA-001 not resolved. Missing tables in public schema: %. Apply supabase/migrations/20260806000001_honeypot_redirect.sql, 20260904000002_insight_queue.sql, 20260904000003_entities_engagement.sql, 20260904000004_finding_alert_policies.sql, 20260904000005_monitor_findings_feedback.sql, 20260906000001_dashboard_operator_authorization.sql before proceeding with the 20260906 remediation migrations.', missing;
    END IF;

    RAISE NOTICE 'DATA-001 verified: all four analyst-workflow tables present.';
END $$;

-- =====================================================================
-- BLOCK 2: broadcast reliability (from 20260906000003) — INTR-001 / DATA-003
-- =====================================================================

ALTER TABLE public.exfiltrated_messages
    ADD COLUMN IF NOT EXISTS broadcast_message_id BIGINT;

ALTER TABLE public.exfiltrated_messages
    ADD COLUMN IF NOT EXISTS broadcast_status TEXT
        CHECK (broadcast_status IN ('pending', 'sent', 'permanent_failed', 'revoked'));

UPDATE public.exfiltrated_messages
   SET broadcast_status = CASE
           WHEN is_broadcasted IS TRUE THEN 'sent'
           ELSE 'pending'
       END
 WHERE broadcast_status IS NULL;

CREATE INDEX IF NOT EXISTS idx_messages_permanent_failed
    ON public.exfiltrated_messages (created_at DESC)
    WHERE broadcast_status = 'permanent_failed';

COMMENT ON COLUMN public.exfiltrated_messages.broadcast_message_id IS
    'Telegram message_id returned by successful sendMessage. When non-NULL, retry logic skips send and marks is_broadcasted=true. See INTR-001.';

COMMENT ON COLUMN public.exfiltrated_messages.broadcast_status IS
    'Explicit broadcast state: pending, sent, permanent_failed, revoked. is_broadcasted alone cannot distinguish successful send from give-up. See DATA-003.';

-- =====================================================================
-- BLOCK 3: media_hashes failure flag + UNIQUE(message_id) (from 20260906000004)
--          INTR-005 / CONC-003
-- =====================================================================

ALTER TABLE public.media_hashes
    ADD COLUMN IF NOT EXISTS is_failure BOOLEAN DEFAULT FALSE;

ALTER TABLE public.media_hashes
    ADD COLUMN IF NOT EXISTS failure_reason TEXT;

UPDATE public.media_hashes
   SET is_failure = TRUE,
       failure_reason = COALESCE(failure_reason, error, 'legacy_sentinel')
 WHERE is_failure = FALSE
   AND sha256 LIKE '__failed__%';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = 'public'
           AND indexname  = 'idx_media_hashes_message_id_unique'
    ) THEN
        CREATE UNIQUE INDEX idx_media_hashes_message_id_unique
            ON public.media_hashes (message_id)
            WHERE is_failure = FALSE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_media_hashes_is_failure
    ON public.media_hashes (is_failure)
    WHERE is_failure = TRUE;

COMMENT ON COLUMN public.media_hashes.is_failure IS
    'True when hash_exfil_media could not download the media. See INTR-005.';

COMMENT ON COLUMN public.media_hashes.failure_reason IS
    'Human-readable failure category (download timeout, file not found, decode error, etc.).';

-- =====================================================================
-- BLOCK 4: audit_logs composite index (from 20260906000005) — PERF-001
-- Note: CREATE INDEX CONCURRENTLY cannot run inside DO $$ blocks or
-- transactions. Supabase SQL editor executes statements individually so
-- CONCURRENTLY is fine here.
-- =====================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_event_type_timestamp
    ON public.audit_logs (event_type, timestamp DESC);

COMMENT ON INDEX public.idx_audit_event_type_timestamp IS
    'PERF-001: composite index for /health/operational and event-type + time-range queries.';

-- =====================================================================
-- DONE
-- Run these SELECTs to verify:
--
-- SELECT column_name FROM information_schema.columns
--  WHERE table_schema='public' AND table_name='exfiltrated_messages'
--    AND column_name IN ('broadcast_message_id','broadcast_status');
--
-- SELECT column_name FROM information_schema.columns
--  WHERE table_schema='public' AND table_name='media_hashes'
--    AND column_name IN ('is_failure','failure_reason');
--
-- SELECT indexname FROM pg_indexes
--  WHERE schemaname='public' AND indexname IN (
--        'idx_messages_permanent_failed',
--        'idx_media_hashes_message_id_unique',
--        'idx_media_hashes_is_failure',
--        'idx_audit_event_type_timestamp');
-- =====================================================================
