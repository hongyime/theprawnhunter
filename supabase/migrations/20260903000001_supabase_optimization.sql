-- ============================================================
-- MIGRATION: Supabase Free Tier Optimization
-- Purpose: Reduce DB size (404MB → ~200MB) and add retention policies
-- safe: PRESERVES all multi-touch redirect data
-- ============================================================

-- ============================================================
-- STEP 1: IMMEDIATE CLEANUP (Free ~140-290MB)
-- ============================================================

-- 1.1 Purge keepalive_log (Free ~5-10MB)
-- Pure heartbeat data, no business value
TRUNCATE TABLE IF EXISTS keepalive_log;

-- 1.2 Delete old broadcasted messages (Free ~100-200MB)
-- Broadcasted messages are already in Telegram, DB copy is redundant
-- Keep only 30 days for potential re-broadcast debugging
DELETE FROM exfiltrated_messages
WHERE is_broadcasted = TRUE
  AND created_at < NOW() - INTERVAL '30 days';

-- 1.3 Delete old audit_logs (Free ~10-30MB)
-- Keep 14 days for debugging, sufficient for operational window
DELETE FROM audit_logs
WHERE timestamp < NOW() - INTERVAL '14 days';

-- 1.4 Delete old honeypot_updates (Free ~20-50MB)
-- Redirected updates older than 30 days are inactive
-- PRESERVE all redirect sequences (multi-touch data)
DELETE FROM honeypot_updates
WHERE received_at < NOW() - INTERVAL '30 days'
  AND redirected_at IS NOT NULL;

-- 1.5 Reclaim space immediately
VACUUM ANALYZE exfiltrated_messages;
VACUUM ANALYZE audit_logs;
VACUUM ANALYZE honeypot_updates;
VACUUM ANALYZE keepalive_log;

-- ============================================================
-- STEP 2: AUTOMATED RETENTION POLICIES (pg_cron)
-- ============================================================

-- 2.1 Keepalive cleanup (daily at 3am)
SELECT cron.schedule(
  'cleanup-keepalive',
  '0 3 * * *',
  $$DELETE FROM keepalive_log WHERE created_at < NOW() - INTERVAL '7 days';$$
);

-- 2.2 Broadcasted messages cleanup (daily at 3am)
SELECT cron.schedule(
  'cleanup-broadcasted-messages',
  '0 3 * * *',
  $$
    DELETE FROM exfiltrated_messages
    WHERE is_broadcasted = TRUE
      AND created_at < NOW() - INTERVAL '30 days';
  $$
);

-- 2.3 Unbroadcasted messages cleanup (daily at 4am)
-- Messages older than 90 days that were never sent will never send
SELECT cron.schedule(
  'cleanup-stale-messages',
  '0 4 * * *',
  $$
    DELETE FROM exfiltrated_messages
    WHERE created_at < NOW() - INTERVAL '90 days'
      AND is_broadcasted = FALSE;
  $$
);

-- 2.4 Audit logs cleanup (daily at 2am)
SELECT cron.schedule(
  'cleanup-audit-logs',
  '0 2 * * *',
  $$DELETE FROM audit_logs WHERE timestamp < NOW() - INTERVAL '14 days';$$
);

-- 2.5 Honeypot updates cleanup (daily at 2am)
-- PRESERVE redirect data: only delete if redirected_at IS NOT NULL (completed)
SELECT cron.schedule(
  'cleanup-honeypot-updates',
  '0 2 * * *',
  $$
    DELETE FROM honeypot_updates
    WHERE received_at < NOW() - INTERVAL '30 days'
      AND redirected_at IS NOT NULL;
  $$
);

-- 2.6 Telemetry indicators cleanup (weekly, Sunday at 5am)
-- Keep indicators for 180 days (valuable for other projects)
SELECT cron.schedule(
  'cleanup-telemetry-indicators',
  '0 5 * * 0',
  $$
    DELETE FROM telemetry_indicators
    WHERE first_seen_at < NOW() - INTERVAL '180 days';
  $$
);

-- ============================================================
-- STEP 3: MESSAGE CONTENT TRUNCATION (Reduce row size)
-- ============================================================

-- Add message content length check (cap at 2000 chars)
-- This prevents future bloat from long messages
COMMENT ON COLUMN exfiltrated_messages.content IS
  'Message text capped at 2000 chars in application layer (flow_tasks.py)';

-- ============================================================
-- STEP 4: MONITOR ENDPOINT OPTIMIZATIONS
-- ============================================================

-- 4.1 Add partial index for webhooks endpoint
-- Faster filtering for webhook discovery
CREATE INDEX IF NOT EXISTS idx_credentials_webhook_url
  ON discovered_credentials(meta->>'webhook_url')
  WHERE meta->>'webhook_url' IS NOT NULL;

-- 4.2 Add counter table for pagination performance
-- Prevent full table scans on monitor endpoints
COMMENT ON TABLE monitor_stats IS
  'Aggregate counters maintained by triggers - prevents COUNT(*) on large tables';

-- ============================================================
-- STEP 5: VERIFICATION QUERIES
-- ============================================================

-- Run these in Supabase SQL Editor to verify cleanup:
-- SELECT pg_size_pretty(pg_total_relation_size('exfiltrated_messages'));
-- SELECT pg_size_pretty(pg_total_relation_size('audit_logs'));
-- SELECT pg_size_pretty(pg_total_relation_size('honeypot_updates'));
-- SELECT COUNT(*) FROM keepalive_log;
-- SELECT cron.jobid, cron.schedule, cron.command FROM cron.job;

-- ============================================================
-- ROLLBACK INSTRUCTIONS (if needed)
-- ============================================================

-- SELECT cron.unschedule('cleanup-keepalive');
-- SELECT cron.unschedule('cleanup-broadcasted-messages');
-- SELECT cron.unschedule('cleanup-stale-messages');
-- SELECT cron.unschedule('cleanup-audit-logs');
-- SELECT cron.unschedule('cleanup-honeypot-updates');
-- SELECT cron.unschedule('cleanup-telemetry-indicators');
