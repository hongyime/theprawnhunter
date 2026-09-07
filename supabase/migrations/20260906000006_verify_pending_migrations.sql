-- =====================================================================
-- Migration: 20260906000006_verify_pending_migrations.sql
-- Purpose:   Verify DATA-001 fix — confirm the four unapplied migrations
--            have been applied to this Supabase project.
-- Trigger:   Operator run BEFORE applying subsequent 20260906000* migrations.
-- Safety:    NO DDL. Reads pg_tables and raises if any expected table is missing.
--            Idempotent. Safe to re-run.
-- =====================================================================

DO $$
DECLARE
    expected_tables text[] := ARRAY['findings','finding_evidence','engagement_events'];
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
-- ROLLBACK
-- No DDL was performed; nothing to roll back. This migration is a check only.
-- =====================================================================
