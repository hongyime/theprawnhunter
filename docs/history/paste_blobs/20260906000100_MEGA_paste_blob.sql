-- =====================================================================
-- 2026-09-06 FULL MEGA MIGRATION BLOB (v2)
-- All non-legacy migrations concatenated in dependency order.
-- Every statement is idempotent (IF NOT EXISTS / DO $$ guards).
-- Already-applied migrations are safe no-ops.
-- =====================================================================


-- =====================================================================
-- SOURCE: 20260802000001_scrape_broadcast_reliability.sql
-- =====================================================================
-- Scrape/broadcast reliability columns for exfiltrated_messages.
-- Idempotent — safe to re-run.
-- Already applied manually in Supabase Dashboard on 2026-08-03.
-- Tracked here so `supabase db push` picks up future changes cleanly.
-- After login+link, baseline with:
--   supabase migration repair --status applied 20260802000001

ALTER TABLE public.exfiltrated_messages
    ADD COLUMN IF NOT EXISTS broadcast_error JSONB DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS broadcast_attempts INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_next_retry
    ON public.exfiltrated_messages(is_broadcasted, next_retry_at)
    WHERE is_broadcasted = FALSE;


-- =====================================================================
-- SOURCE: 20260803000001_broadcasted_at.sql
-- =====================================================================
-- Migration: add broadcasted_at column to exfiltrated_messages
-- Purpose: capture the exact timestamp of successful broadcast so
-- flow.exfil_latency_report can compute true broadcasted_at - created_at
-- latency instead of the current upper-bound proxy (NOW - created_at).
--
-- Idempotent — safe to re-run.
-- Note: no updated_at column exists on this table, so no backfill of
-- legacy rows is possible. The new column starts NULL for existing rows
-- and gets populated from broadcasts going forward.

ALTER TABLE public.exfiltrated_messages
    ADD COLUMN IF NOT EXISTS broadcasted_at TIMESTAMPTZ DEFAULT NULL;

-- Index on broadcasted_at for latency queries (only on rows that succeeded)
CREATE INDEX IF NOT EXISTS idx_messages_broadcasted_at
    ON public.exfiltrated_messages(broadcasted_at)
    WHERE broadcasted_at IS NOT NULL;


-- =====================================================================
-- SOURCE: 20260803000010_message_fts.sql
-- =====================================================================
-- Migration: pg_trgm-backed full-text search over exfiltrated_messages.content
-- Purpose: enable near-instant LIKE '%pattern%' queries across 283k+ messages
-- for OSINT hunting (bitcoin, phishing keywords, phone numbers, etc.)
--
-- Uses trigram GIN index — much better than tsvector for arbitrary substring
-- matching, and doesn't require language-specific stemming.
--
-- Idempotent — safe to re-run.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_messages_content_trgm
    ON public.exfiltrated_messages
    USING GIN (content gin_trgm_ops);

-- Also index sender_name for "find all messages from X" queries
CREATE INDEX IF NOT EXISTS idx_messages_sender_trgm
    ON public.exfiltrated_messages
    USING GIN (sender_name gin_trgm_ops);


-- =====================================================================
-- SOURCE: 20260803000011_media_hashes.sql
-- =====================================================================
-- Migration: media forensics table
-- Purpose: track SHA-256 + perceptual hash of exfiltrated media so we can
-- detect the same photo/document being sent from multiple compromised bots
-- (which would identify a common threat-actor sender or reused payload).
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS public.media_hashes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES public.exfiltrated_messages(id) ON DELETE CASCADE,
    credential_id UUID REFERENCES public.discovered_credentials(id) ON DELETE SET NULL,
    sha256 TEXT NOT NULL,
    phash TEXT DEFAULT NULL,
    file_size_bytes INT DEFAULT NULL,
    mime_type TEXT DEFAULT NULL,
    media_type TEXT DEFAULT NULL,
    downloaded_at TIMESTAMPTZ DEFAULT NOW(),
    error TEXT DEFAULT NULL,
    UNIQUE (message_id)
);

-- Duplicate detection: find same photo across bots
CREATE INDEX IF NOT EXISTS idx_media_hashes_sha256
    ON public.media_hashes(sha256);
CREATE INDEX IF NOT EXISTS idx_media_hashes_phash
    ON public.media_hashes(phash)
    WHERE phash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_hashes_credential
    ON public.media_hashes(credential_id);


-- =====================================================================
-- SOURCE: 20260803000012_honeypot.sql
-- =====================================================================
-- Migration: honeypot_updates table
-- Purpose: store incoming webhook POSTs captured after we take over a
-- third-party's stolen webhook and re-register it to point at us.
-- Only populated when HONEYPOT_MODE=True + public HTTPS endpoint deployed.
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS public.honeypot_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    credential_id UUID REFERENCES public.discovered_credentials(id) ON DELETE SET NULL,
    update_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ DEFAULT NOW(),
    source_ip TEXT DEFAULT NULL,
    processed_at TIMESTAMPTZ DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_honeypot_credential
    ON public.honeypot_updates(credential_id);
CREATE INDEX IF NOT EXISTS idx_honeypot_received
    ON public.honeypot_updates(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_honeypot_type
    ON public.honeypot_updates(update_type);
CREATE INDEX IF NOT EXISTS idx_honeypot_unprocessed
    ON public.honeypot_updates(received_at)
    WHERE processed_at IS NULL;


-- =====================================================================
-- SOURCE: 20260804000001_system_state.sql
-- =====================================================================
-- Migration: system_state key-value table
-- Purpose: a small, versionless place to persist singleton state like
-- 'pinned_readme_msg_id', 'canary_last_run', 'last_takeover_alert_at', etc.
-- Prevents scattered custom tables for one-off flags.
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS public.system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);


-- =====================================================================
-- SOURCE: 20260805000001_account_membership_admin.sql
-- =====================================================================
-- Migration: track telegram_user_id + admin-promoted state per session account
-- Purpose: enable membership audit (verify each session's account is still in
-- the monitor group) and auto-promote joining sessions with minimal admin
-- permissions (invite bots only — no group-edit/kick/pin/promote rights).
--
-- Idempotent — safe to re-run.

ALTER TABLE public.telegram_accounts
    ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS is_admin_promoted BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS in_monitor_group BOOLEAN DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS last_membership_check_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_accounts_telegram_user_id
    ON public.telegram_accounts(telegram_user_id)
    WHERE telegram_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_accounts_promoted
    ON public.telegram_accounts(is_admin_promoted, status)
    WHERE status = 'active';


-- =====================================================================
-- SOURCE: 20260805000002_sender_user_id.sql
-- =====================================================================
-- Migration: add sender_user_id to exfiltrated_messages
-- Purpose: enable attribution graph — link the same Telegram user_id
-- across multiple compromised bots to identify serial victims and
-- coordinated operator patterns.
--
-- Currently we only store sender_name (display name) which is not unique
-- and can't be joined reliably. sender_user_id is the immutable numeric
-- Telegram user ID from message.from.id.
--
-- Idempotent — safe to re-run.

ALTER TABLE public.exfiltrated_messages
    ADD COLUMN IF NOT EXISTS sender_user_id BIGINT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_sender_user_id
    ON public.exfiltrated_messages(sender_user_id)
    WHERE sender_user_id IS NOT NULL;


-- =====================================================================
-- SOURCE: 20260806000001_honeypot_redirect.sql
-- =====================================================================
-- Migration: add redirect tracking to honeypot_updates
-- Purpose: track which captured users have been sent a redirect message
-- to the onboard bot, preventing duplicate sends.
--
-- Idempotent — safe to re-run.

ALTER TABLE public.honeypot_updates
    ADD COLUMN IF NOT EXISTS redirected_at TIMESTAMPTZ DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS redirected_bot TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS redirect_error TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS sender_user_id BIGINT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_honeypot_unredir
    ON public.honeypot_updates(received_at)
    WHERE redirected_at IS NULL AND update_type = 'message';

CREATE INDEX IF NOT EXISTS idx_honeypot_sender
    ON public.honeypot_updates(sender_user_id)
    WHERE sender_user_id IS NOT NULL;


-- =====================================================================
-- SOURCE: 20260828000001_monitor_stats.sql
-- =====================================================================
-- Migration: maintained monitor stats counters
-- Purpose: keep /monitor/stats O(1) as exfiltrated_messages grows.

CREATE TABLE IF NOT EXISTS public.monitor_stats (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE,
    credentials_total BIGINT NOT NULL DEFAULT 0,
    credentials_active BIGINT NOT NULL DEFAULT 0,
    messages_exfiltrated BIGINT NOT NULL DEFAULT 0,
    messages_broadcasted BIGINT NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT monitor_stats_singleton CHECK (id = TRUE)
);

INSERT INTO public.monitor_stats (
    id,
    credentials_total,
    credentials_active,
    messages_exfiltrated,
    messages_broadcasted,
    refreshed_at
)
SELECT
    TRUE,
    (SELECT COUNT(*) FROM public.discovered_credentials),
    (SELECT COUNT(*) FROM public.discovered_credentials WHERE status = 'active'),
    (SELECT COUNT(*) FROM public.exfiltrated_messages),
    (SELECT COUNT(*) FROM public.exfiltrated_messages WHERE is_broadcasted = TRUE),
    NOW()
ON CONFLICT (id) DO UPDATE SET
    credentials_total = EXCLUDED.credentials_total,
    credentials_active = EXCLUDED.credentials_active,
    messages_exfiltrated = EXCLUDED.messages_exfiltrated,
    messages_broadcasted = EXCLUDED.messages_broadcasted,
    refreshed_at = EXCLUDED.refreshed_at;

CREATE OR REPLACE FUNCTION public.monitor_stats_credentials_delta()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE public.monitor_stats
        SET
            credentials_total = credentials_total + 1,
            credentials_active = credentials_active + CASE WHEN NEW.status = 'active' THEN 1 ELSE 0 END,
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE public.monitor_stats
        SET
            credentials_total = GREATEST(credentials_total - 1, 0),
            credentials_active = GREATEST(credentials_active - CASE WHEN OLD.status = 'active' THEN 1 ELSE 0 END, 0),
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' AND OLD.status IS DISTINCT FROM NEW.status THEN
        UPDATE public.monitor_stats
        SET
            credentials_active = GREATEST(
                credentials_active
                - CASE WHEN OLD.status = 'active' THEN 1 ELSE 0 END
                + CASE WHEN NEW.status = 'active' THEN 1 ELSE 0 END,
                0
            ),
            refreshed_at = NOW()
        WHERE id = TRUE;
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.monitor_stats_messages_delta()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE public.monitor_stats
        SET
            messages_exfiltrated = messages_exfiltrated + 1,
            messages_broadcasted = messages_broadcasted + CASE WHEN NEW.is_broadcasted = TRUE THEN 1 ELSE 0 END,
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE public.monitor_stats
        SET
            messages_exfiltrated = GREATEST(messages_exfiltrated - 1, 0),
            messages_broadcasted = GREATEST(messages_broadcasted - CASE WHEN OLD.is_broadcasted = TRUE THEN 1 ELSE 0 END, 0),
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' AND OLD.is_broadcasted IS DISTINCT FROM NEW.is_broadcasted THEN
        UPDATE public.monitor_stats
        SET
            messages_broadcasted = GREATEST(
                messages_broadcasted
                - CASE WHEN OLD.is_broadcasted = TRUE THEN 1 ELSE 0 END
                + CASE WHEN NEW.is_broadcasted = TRUE THEN 1 ELSE 0 END,
                0
            ),
            refreshed_at = NOW()
        WHERE id = TRUE;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_monitor_stats_credentials_delta ON public.discovered_credentials;
CREATE TRIGGER trg_monitor_stats_credentials_delta
AFTER INSERT OR UPDATE OF status OR DELETE ON public.discovered_credentials
FOR EACH ROW EXECUTE FUNCTION public.monitor_stats_credentials_delta();

DROP TRIGGER IF EXISTS trg_monitor_stats_messages_delta ON public.exfiltrated_messages;
CREATE TRIGGER trg_monitor_stats_messages_delta
AFTER INSERT OR UPDATE OF is_broadcasted OR DELETE ON public.exfiltrated_messages
FOR EACH ROW EXECUTE FUNCTION public.monitor_stats_messages_delta();

CREATE INDEX IF NOT EXISTS idx_messages_broadcasted_true
    ON public.exfiltrated_messages(is_broadcasted)
    WHERE is_broadcasted = TRUE;

CREATE OR REPLACE FUNCTION public.get_monitor_stats()
RETURNS TABLE (
    credentials_total BIGINT,
    credentials_active BIGINT,
    messages_exfiltrated BIGINT,
    messages_broadcasted BIGINT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT
        ms.credentials_total,
        ms.credentials_active,
        ms.messages_exfiltrated,
        ms.messages_broadcasted
    FROM public.monitor_stats AS ms
    WHERE ms.id = TRUE;
$$;

REVOKE ALL ON FUNCTION public.get_monitor_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_monitor_stats() TO service_role;


-- =====================================================================
-- SOURCE: 20260829000001_multi_touch_redirects.sql
-- =====================================================================
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


-- =====================================================================
-- SOURCE: 20260903000001_supabase_optimization.sql
-- =====================================================================
-- ============================================================
-- MIGRATION: Safe retention foundations
-- Purpose: add durable archive/summary structures without deleting data.
--
-- This migration is deliberately schema-only. Applying or reapplying it:
--   * never purges, truncates, vacuums, or schedules cleanup;
--   * never assumes pg_cron is installed;
--   * preserves raw history until an operator runs the separate dry-run-first
--     procedure in database/operations/retention_cleanup.sql.
-- ============================================================

-- Generic, append-safe archive for full source-row snapshots. Keeping archive
-- rows in the same database preserves recoverability but may not materially
-- reduce total storage; export verified archive rows to durable object storage
-- before removing them from this table when database size is the constraint.
CREATE TABLE IF NOT EXISTS public.retention_archive (
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_recorded_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archive_batch_id UUID NOT NULL,
    PRIMARY KEY (source_table, source_id)
);

CREATE INDEX IF NOT EXISTS idx_retention_archive_recorded_at
    ON public.retention_archive(source_table, source_recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_retention_archive_batch
    ON public.retention_archive(archive_batch_id);

COMMENT ON TABLE public.retention_archive IS
    'Full JSON snapshots written before optional retention deletion. '
    'Service-role only; see database/operations/retention_cleanup.sql.';

ALTER TABLE public.retention_archive ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.retention_archive FROM PUBLIC;
REVOKE ALL ON public.retention_archive FROM anon;
REVOKE ALL ON public.retention_archive FROM authenticated;

CREATE TABLE IF NOT EXISTS public.retention_cleanup_runs (
    run_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    requested_by TEXT NOT NULL DEFAULT CURRENT_USER,
    archive_batch_id UUID NOT NULL,
    results JSONB NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE public.retention_cleanup_runs IS
    'Audit record for explicitly confirmed archive-before-delete cleanup runs.';

ALTER TABLE public.retention_cleanup_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.retention_cleanup_runs FROM PUBLIC;
REVOKE ALL ON public.retention_cleanup_runs FROM anon;
REVOKE ALL ON public.retention_cleanup_runs FROM authenticated;

-- ============================================================
-- STEP 2: DOCUMENT APPLICATION-LAYER SIZE CONTROL
-- ============================================================

-- Add message content length check (cap at 2000 chars)
-- This prevents future bloat from long messages
COMMENT ON COLUMN public.exfiltrated_messages.content IS
  'Message text capped at 2000 chars in application layer (flow_tasks.py)';

-- ============================================================
-- STEP 3: MONITOR ENDPOINT OPTIMIZATIONS
-- ============================================================

-- 4.1 Add partial index for webhooks endpoint
-- Faster filtering for webhook discovery
CREATE INDEX IF NOT EXISTS idx_credentials_webhook_url
  ON public.discovered_credentials((meta->>'webhook_url'))
  WHERE meta->>'webhook_url' IS NOT NULL;

-- 4.2 Add counter table for pagination performance
-- Prevent full table scans on monitor endpoints
COMMENT ON TABLE public.monitor_stats IS
  'Aggregate counters maintained by triggers - prevents COUNT(*) on large tables';

-- ============================================================
-- STEP 4: DURABLE OPERATOR HISTORY
-- ============================================================

-- 7.1 TIER 2: DURABLE FINDING SUMMARIES
-- Purpose: Operator-visible history (2-year retention)
-- Storage: ~50KB per 1000 findings
CREATE TABLE IF NOT EXISTS public.finding_summaries (
    finding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 10),
    entity_type VARCHAR(64),
    entity_value TEXT,
    credential_id UUID REFERENCES public.discovered_credentials(id) ON DELETE SET NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    occurrence_count INTEGER DEFAULT 1,
    operator_notes TEXT,
    disposition VARCHAR(32) DEFAULT 'new' CHECK (disposition IN ('new','useful','noise','suppressed','escalated')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_finding_summaries_type
    ON public.finding_summaries(finding_type);
CREATE INDEX IF NOT EXISTS idx_finding_summaries_entity
    ON public.finding_summaries(entity_type, entity_value);
CREATE INDEX IF NOT EXISTS idx_finding_summaries_time
    ON public.finding_summaries(first_seen_at DESC);

-- 7.2 TIER 3: EVIDENCE PROVENANCE
-- Purpose: Drill-down without full raw messages
CREATE TABLE IF NOT EXISTS public.finding_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id UUID REFERENCES public.finding_summaries(finding_id) ON DELETE CASCADE,
    message_id UUID,
    evidence_type VARCHAR(64) NOT NULL,
    evidence_hash VARCHAR(128) NOT NULL,
    evidence_snippet TEXT,
    credential_id UUID,
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
    evidence_count INTEGER DEFAULT 1,
    raw_message_available BOOLEAN DEFAULT TRUE,
    CONSTRAINT unique_evidence_hash UNIQUE(finding_id, evidence_hash)
);

CREATE INDEX IF NOT EXISTS idx_finding_evidence_finding
    ON public.finding_evidence(finding_id);
CREATE INDEX IF NOT EXISTS idx_finding_evidence_message
    ON public.finding_evidence(message_id) WHERE message_id IS NOT NULL;

-- No cleanup is installed or run here. Operators must preview and explicitly
-- confirm database/operations/retention_cleanup.sql. VACUUM, if desired after
-- a confirmed cleanup, must be run separately because it cannot execute inside
-- the migration transaction.


-- =====================================================================
-- SOURCE: 20260903000003_collection_yield_score.sql
-- =====================================================================
-- ============================================================
-- PLAN ITEM 2 — Rename confidence_score → collection_yield_score
-- Add separate finding score fields (confidence/severity/priority/explanation)
-- Add deterministic calculate_finding_priority() function
--
-- CONTEXT
-- The existing `confidence_score` column on discovered_credentials is a
-- STORED generated column that reads from meta->>'confidence_score'. The
-- score rewards resolved chat IDs, configured webhooks, group type,
-- member count, and username availability — it measures likely data
-- yield, NOT intelligence confidence. This migration renames it to
-- `collection_yield_score` while keeping the old column as a live alias
-- during transition, and introduces real evidence-quality fields on
-- finding_summaries.
--
-- IDEMPOTENCE
-- Every DDL uses IF NOT EXISTS / IF EXISTS / DO-block existence checks
-- so this migration is safe to re-run.
-- ============================================================


-- ------------------------------------------------------------
-- 1. discovered_credentials.collection_yield_score
--    New STORED GENERATED column. Reads from meta->>'collection_yield_score'
--    with a fallback to the legacy meta->>'confidence_score' key so writers
--    that have not migrated yet still populate the new column.
-- ------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'discovered_credentials'
          AND a.attname = 'collection_yield_score'
          AND a.attnum > 0
          AND NOT a.attisdropped
    ) THEN
        ALTER TABLE public.discovered_credentials
        ADD COLUMN collection_yield_score INTEGER GENERATED ALWAYS AS (
            CASE
                WHEN meta ? 'collection_yield_score'
                  AND jsonb_typeof(meta->'collection_yield_score') = 'number'
                THEN (meta->>'collection_yield_score')::int
                WHEN meta ? 'confidence_score'
                  AND jsonb_typeof(meta->'confidence_score') = 'number'
                THEN (meta->>'confidence_score')::int
                ELSE NULL
            END
        ) STORED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_discovered_credentials_collection_yield_score
    ON public.discovered_credentials (collection_yield_score DESC NULLS LAST)
    WHERE collection_yield_score IS NOT NULL;

COMMENT ON COLUMN public.discovered_credentials.collection_yield_score IS
    'Collection-yield score (0-100). Rewards resolved chat IDs, webhooks, '
    'group type, member count, username availability. This is NOT '
    'intelligence confidence — see finding_summaries.confidence for that. '
    'Legacy alias: confidence_score.';

COMMENT ON COLUMN public.discovered_credentials.confidence_score IS
    'DEPRECATED alias for collection_yield_score. Retained for backwards '
    'compatibility with existing readers. Do not use in new code.';


-- ------------------------------------------------------------
-- 2. Public view — expose both column names during transition
--    Preserve existing column order, append new column last.
--    Security: REVOKE from PUBLIC/anon, GRANT to authenticated only.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW public.discovered_credentials_public AS
SELECT
    id,
    created_at,
    source,
    status,
    meta,
    confidence_score,
    chat_member_count,
    collection_yield_score
FROM public.discovered_credentials;

REVOKE SELECT ON public.discovered_credentials_public FROM PUBLIC;
REVOKE SELECT ON public.discovered_credentials_public FROM anon;
GRANT SELECT ON public.discovered_credentials_public TO authenticated;



-- ------------------------------------------------------------
-- 3. finding_summaries — add evidence-quality fields
--    Table was created in 20260903000001 with severity + priority already.
--    Add: confidence (0-1), explanation (required).
--    Guard for environments where finding_summaries has not yet been
--    created (earlier migration syntax was broken).
-- ------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'finding_summaries'
    ) THEN
        CREATE TABLE public.finding_summaries (
            finding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            finding_type VARCHAR(64) NOT NULL,
            severity VARCHAR(16) NOT NULL CHECK (severity IN ('low','medium','high','critical')),
            priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 10),
            entity_type VARCHAR(64),
            entity_value TEXT,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            occurrence_count INTEGER DEFAULT 1,
            operator_notes TEXT,
            disposition VARCHAR(32) DEFAULT 'new'
                CHECK (disposition IN ('new','useful','noise','suppressed','escalated')),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    END IF;
END $$;

-- confidence: evidence-quality score in [0, 1].
ALTER TABLE public.finding_summaries
    ADD COLUMN IF NOT EXISTS confidence REAL NOT NULL DEFAULT 0.5;

-- explanation: human-readable justification (required).
-- Idempotent four-step sequence: add nullable → backfill → set NOT NULL → add CHECK.
-- Step 1: Add column nullable if missing.
ALTER TABLE public.finding_summaries
    ADD COLUMN IF NOT EXISTS explanation TEXT;

-- Step 2: Backfill NULL/blank legacy rows with deterministic explanation.
-- Uses available finding fields to construct a human-readable message.
UPDATE public.finding_summaries
SET explanation = format(
    'Finding %s: %s (entity=%s, severity=%s, priority=%s, confidence=%s, occurrences=%s)',
    finding_type,
    COALESCE(entity_value, 'unknown'),
    COALESCE(entity_type, 'unknown'),
    severity,
    priority,
    COALESCE(confidence::text, '0.5'),
    COALESCE(occurrence_count::text, '1')
)
WHERE explanation IS NULL OR btrim(explanation) = '';

-- Step 3: Drop the default before making the column NOT NULL.
-- This prevents invalid rows from being created with DEFAULT ''.
ALTER TABLE public.finding_summaries
    ALTER COLUMN explanation DROP DEFAULT;

-- Step 3.5: Make the column NOT NULL after backfill and dropping default.
ALTER TABLE public.finding_summaries
    ALTER COLUMN explanation SET NOT NULL;

-- Step 4: Add CHECK constraint enforcing non-blank content.
-- Constraint name is schema/table-scoped: public.finding_summaries_explanation_required.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = 'public'
          AND c.conname = 'finding_summaries_explanation_required'
          AND c.conrelid = 'public.finding_summaries'::regclass
    ) THEN
        ALTER TABLE public.finding_summaries
        ADD CONSTRAINT finding_summaries_explanation_required
        CHECK (btrim(explanation) <> '');
    END IF;
END $$;

-- Add CHECK for confidence range if not already present (schema-scoped guard).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = 'public'
          AND c.conname = 'finding_summaries_confidence_range'
          AND c.conrelid = 'public.finding_summaries'::regclass
    ) THEN
        ALTER TABLE public.finding_summaries
        ADD CONSTRAINT finding_summaries_confidence_range
        CHECK (confidence >= 0.0 AND confidence <= 1.0);
    END IF;
END $$;
COMMENT ON COLUMN public.finding_summaries.confidence IS
    'Evidence-quality confidence in [0.0, 1.0]. This is intelligence '
    'confidence — NOT collection yield (see discovered_credentials.'
    'collection_yield_score for that).';

COMMENT ON COLUMN public.finding_summaries.severity IS
    'Potential-impact bucket: low | medium | high | critical.';

COMMENT ON COLUMN public.finding_summaries.priority IS
    'What to review now, integer 1-10 (10 = review first). Derived from '
    'calculate_finding_priority().';

COMMENT ON COLUMN public.finding_summaries.explanation IS
    'Human-readable justification for severity/priority. Required.';


-- ------------------------------------------------------------
-- 4. calculate_finding_priority()
--    Deterministic PL/pgSQL function. Given (finding_type, entity_type,
--    evidence_count, confidence) returns (severity, priority, explanation).
--    Same inputs => same outputs.
-- ------------------------------------------------------------
DROP FUNCTION IF EXISTS public.calculate_finding_priority(TEXT, TEXT, INTEGER, REAL);

CREATE OR REPLACE FUNCTION public.calculate_finding_priority(
    p_finding_type TEXT,
    p_entity_type  TEXT,
    p_evidence_count INTEGER,
    p_confidence   REAL
) RETURNS TABLE (
    severity    VARCHAR(16),
    priority    INTEGER,
    explanation TEXT
)
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    v_severity   VARCHAR(16);
    v_priority   INTEGER;
    v_base       INTEGER;
    v_ev         INTEGER;
    v_conf       REAL;
BEGIN
    -- Input validation — return safe deterministic fallback on bad input.
    IF p_finding_type IS NULL OR p_finding_type = '' THEN
        RETURN QUERY SELECT
            'low'::VARCHAR(16),
            1::INTEGER,
            'invalid input: finding_type required'::TEXT;
        RETURN;
    END IF;

    v_ev   := GREATEST(COALESCE(p_evidence_count, 0), 0);
    v_conf := GREATEST(LEAST(COALESCE(p_confidence, 0.0), 1.0), 0.0);

    -- Severity: deterministic bands over (confidence, evidence_count).
    IF v_conf >= 0.90 AND v_ev >= 5 THEN
        v_severity := 'critical';
    ELSIF v_conf >= 0.75 AND v_ev >= 3 THEN
        v_severity := 'high';
    ELSIF v_conf >= 0.50 THEN
        v_severity := 'medium';
    ELSE
        v_severity := 'low';
    END IF;

    -- Base priority anchored on finding_type family.
    -- Canonical required finding types: credential_exposure,
    -- infrastructure_cluster, cross_bot_pattern.
    -- Legacy aliases retained for backwards compatibility.
    v_base := CASE p_finding_type
        WHEN 'credential_exposure'   THEN 8  -- canonical
        WHEN 'active_credential'      THEN 8  -- legacy alias
        WHEN 'exposed_credential'     THEN 7  -- legacy alias
        WHEN 'webhook_hijack'         THEN 7
        WHEN 'honeypot_capture'       THEN 6
        WHEN 'operator_cluster'       THEN 6
        WHEN 'infrastructure_cluster' THEN 5  -- canonical
        WHEN 'infrastructure_reuse'   THEN 5  -- legacy alias
        WHEN 'cross_bot_pattern'      THEN 4  -- canonical
        WHEN 'media_duplicate'        THEN 4
        WHEN 'attribution_link'       THEN 4
        WHEN 'passive_indicator'      THEN 3
        ELSE 5
    END;

    -- Severity boost.
    v_priority := v_base + CASE v_severity
        WHEN 'critical' THEN 2
        WHEN 'high'     THEN 1
        WHEN 'medium'   THEN 0
        ELSE -1
    END;

    -- Entity-type nudge (channel/supergroup infra beats DMs).
    IF p_entity_type IN ('supergroup', 'channel') THEN
        v_priority := v_priority + 1;
    END IF;

    -- Clamp to [1, 10].
    v_priority := GREATEST(LEAST(v_priority, 10), 1);

    RETURN QUERY SELECT
        v_severity,
        v_priority,
        format(
            'type=%s entity=%s evidence=%s confidence=%s => severity=%s priority=%s',
            p_finding_type,
            COALESCE(p_entity_type, 'unknown'),
            v_ev,
            ROUND(v_conf::NUMERIC, 2),
            v_severity,
            v_priority
        )::TEXT;
END;
$$;

COMMENT ON FUNCTION public.calculate_finding_priority(TEXT, TEXT, INTEGER, REAL) IS
    'Deterministic finding scorer. Given finding_type, entity_type, '
    'evidence_count, confidence[0..1], returns (severity, priority, '
    'explanation). Same inputs always produce same outputs. IMMUTABLE. '
    'Canonical finding types: credential_exposure, infrastructure_cluster, '
    'cross_bot_pattern. Legacy aliases retained for backwards compatibility.';


-- =====================================================================
-- SOURCE: 20260903000004_rls_hardening.sql
-- =====================================================================
-- Plan Item 1: Evidence Surface Hardening (VALID PostgreSQL)
-- Revoke ALL raw access from anon, authenticated, and PUBLIC.
-- Authenticated operators must use evidence_redacted view.
-- Service role retains full bypass access.

-- ============================================
-- STEP 1: DROP POLICIES THAT ALLOW RAW ACCESS
-- ============================================
DROP POLICY IF EXISTS "Authenticated Read Access" ON public.exfiltrated_messages;
DROP POLICY IF EXISTS "Anon Read Access" ON public.exfiltrated_messages;

-- ============================================
-- STEP 2: REVOKE ALL RAW TABLE ACCESS
-- ============================================
-- Revoke from PUBLIC role (catches any implicit grants)
REVOKE ALL ON public.exfiltrated_messages FROM PUBLIC;
-- Revoke from anon and authenticated explicitly
REVOKE ALL ON public.exfiltrated_messages FROM anon;
REVOKE ALL ON public.exfiltrated_messages FROM authenticated;

-- ============================================
-- STEP 3: SERVICE ROLE POLICY (safe, idempotent)
-- ============================================
DROP POLICY IF EXISTS "Service Role Full Access" ON public.exfiltrated_messages;

CREATE POLICY "Service Role Full Access"
ON public.exfiltrated_messages
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- ============================================
-- STEP 4: RECREATE EVIDENCE_REDACTED VIEW
-- ============================================
DROP VIEW IF EXISTS public.evidence_redacted;

CREATE VIEW public.evidence_redacted
WITH (security_invoker = false) AS
SELECT
    id,
    credential_id,
    -- Irreversible pseudonym using md5 (portable, no pgcrypto required)
    'user_' || substring(md5(sender_name || 'salt_pr4wn_hunt3r'), 1, 8) AS sender_pseudonym,
    -- Mask token patterns BEFORE truncation to prevent boundary leaks
    regexp_replace(
        left(
            regexp_replace(
                COALESCE(content, ''),
                '\d{8,10}:[A-Za-z0-9_-]{30,}',
                '[TOKEN]',
                'g'
            ),
            500
        ),
        '\d{8,10}:[A-Za-z0-9_-]{30,}',
        '[TOKEN]',
        'g'
    ) AS content,
    media_type,
    is_broadcasted,
    created_at
FROM public.exfiltrated_messages;

-- ============================================
-- STEP 5: GRANT REDACTED VIEW TO AUTHENTICATED ONLY
-- ============================================
-- Revoke from PUBLIC and anon (belt-and-suspenders)
REVOKE ALL ON public.evidence_redacted FROM PUBLIC;
REVOKE ALL ON public.evidence_redacted FROM anon;

-- Grant to authenticated operators only
GRANT SELECT ON public.evidence_redacted TO authenticated;

-- Comment documenting the view
COMMENT ON VIEW public.evidence_redacted IS
    'Redacted evidence view for authenticated operators: content token-masked then truncated to 500 chars, sender replaced with irreversible md5 pseudonym, telegram_msg_id omitted, file_meta and broadcast_error omitted. AUTHENTICATED-ONLY. See supabase/migrations/20260903000004_rls_hardening.sql.';

-- ============================================
-- VERIFICATION QUERIES
-- ============================================
-- Confirm no raw policies for anon/authenticated on exfiltrated_messages
SELECT 'Should return 0 rows' AS check,
       COUNT(*) AS raw_access_policies
FROM pg_policies
WHERE tablename = 'exfiltrated_messages'
  AND schemaname = 'public'
  AND roles::text !~ 'service_role';

-- Confirm evidence_redacted grants (authenticated only)
SELECT table_schema, table_name, privilege_type, grantee
FROM information_schema.role_table_grants
WHERE table_name = 'evidence_redacted'
  AND table_schema = 'public'
ORDER BY grantee;


-- =====================================================================
-- SOURCE: 20260903000005_discovered_credentials_public_authenticated.sql
-- =====================================================================
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


-- =====================================================================
-- SOURCE: 20260904000001_disable_legacy_retention_jobs.sql
-- =====================================================================
-- Disable destructive retention jobs that may have been installed by an
-- earlier version of 20260903000001_supabase_optimization.sql.
--
-- New installations never create these jobs. This forward migration protects
-- already-upgraded environments and is safe when pg_cron is absent or when the
-- jobs have already been removed.
DO $disable_legacy_retention_jobs$
DECLARE
    legacy_job RECORD;
BEGIN
    IF to_regclass('cron.job') IS NULL THEN
        RETURN;
    END IF;

    FOR legacy_job IN
        SELECT jobid
        FROM cron.job
        WHERE jobname = ANY (ARRAY[
            'cleanup-keepalive',
            'cleanup-broadcasted-messages',
            'cleanup-stale-messages',
            'cleanup-audit-logs',
            'cleanup-honeypot-updates',
            'cleanup-telemetry-indicators',
            'cleanup-finding-summaries',
            'cleanup-finding-evidence'
        ])
    LOOP
        PERFORM cron.unschedule(legacy_job.jobid);
    END LOOP;
END
$disable_legacy_retention_jobs$;


-- =====================================================================
-- SOURCE: 20260904000002_insight_queue.sql
-- =====================================================================
-- Plan Items 3/5/8: persistent, explainable Insight Queue foundations.
--
-- This migration preserves the legacy finding_summaries/finding_evidence data,
-- moves the evidence foreign key to the canonical findings table, and exposes
-- only authenticated, redacted analyst surfaces. Producers write atomically
-- through public.upsert_finding(); analyst actions use
-- public.record_finding_feedback().

CREATE TABLE IF NOT EXISTS public.findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL CHECK (type IN (
        'credential_exposure',
        'infrastructure_cluster',
        'cross_bot_pattern'
    )),
    canonical_key TEXT NOT NULL CHECK (btrim(canonical_key) <> ''),
    title TEXT NOT NULL CHECK (btrim(title) <> ''),
    summary TEXT NOT NULL CHECK (btrim(summary) <> ''),
    why_it_matters TEXT NOT NULL CHECK (btrim(why_it_matters) <> ''),
    recommended_action TEXT NOT NULL CHECK (btrim(recommended_action) <> ''),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    priority SMALLINT NOT NULL CHECK (priority BETWEEN 1 AND 10),
    score_explanation JSONB NOT NULL CHECK (jsonb_typeof(score_explanation) = 'object'),
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN (
        'new', 'triaged', 'in_progress', 'resolved', 'dismissed', 'suppressed'
    )),
    assignee TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    material_version INTEGER NOT NULL DEFAULT 1 CHECK (material_version >= 1),
    last_material_change_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT findings_seen_order CHECK (last_seen_at >= first_seen_at),
    CONSTRAINT findings_type_canonical_key UNIQUE (type, canonical_key)
);

CREATE INDEX IF NOT EXISTS idx_findings_queue
    ON public.findings(status, priority DESC, last_material_change_at DESC);
CREATE INDEX IF NOT EXISTS idx_findings_type_recent
    ON public.findings(type, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_findings_assignee
    ON public.findings(assignee, status) WHERE assignee IS NOT NULL;

-- Preserve nullable legacy evidence rather than deleting it. The sentinel is
-- visible to operators as a migration artifact and can be dispositioned.
INSERT INTO public.findings (
    id, type, canonical_key, title, summary, why_it_matters,
    recommended_action, confidence, severity, priority, score_explanation,
    first_seen_at, last_seen_at
) VALUES (
    '00000000-0000-0000-0000-000000000000',
    'credential_exposure',
    'legacy:unassigned-evidence',
    'Legacy evidence awaiting attribution',
    'Evidence created before the Insight Queue had a required finding relationship.',
    'The source row is preserved, but its original finding relationship was absent.',
    'Review the evidence provenance and attach it to the appropriate finding.',
    0.1,
    'low',
    1,
    '{"version":1,"migration":"legacy_unassigned_evidence"}'::jsonb,
    NOW(),
    NOW()
)
ON CONFLICT (id) DO NOTHING;

-- Backfill legacy summary rows without overwriting future analyst decisions.
INSERT INTO public.findings (
    id, type, canonical_key, title, summary, why_it_matters,
    recommended_action, confidence, severity, priority, score_explanation,
    status, first_seen_at, last_seen_at, evidence_count, created_at, updated_at
)
SELECT
    summary.finding_id,
    CASE
        WHEN summary.finding_type IN ('credential_exposure', 'bot_credential', 'credential')
            THEN 'credential_exposure'
        WHEN summary.finding_type IN ('infrastructure_cluster', 'c2_cluster', 'webhook_cluster')
            THEN 'infrastructure_cluster'
        ELSE 'cross_bot_pattern'
    END,
    'legacy:' || summary.finding_id::text,
    'Legacy ' || replace(summary.finding_type, '_', ' ') || ' finding',
    summary.explanation,
    'This finding was preserved from the pre-queue summary history.',
    'Review the linked evidence and record an analyst disposition.',
    LEAST(1.0, GREATEST(0.0, summary.confidence)),
    summary.severity,
    summary.priority,
    jsonb_build_object(
        'version', 1,
        'migration', 'finding_summaries',
        'legacy_explanation', summary.explanation,
        'legacy_type', summary.finding_type
    ),
    CASE summary.disposition
        WHEN 'suppressed' THEN 'suppressed'
        WHEN 'escalated' THEN 'in_progress'
        WHEN 'noise' THEN 'dismissed'
        ELSE 'new'
    END,
    summary.first_seen_at,
    summary.last_seen_at,
    (SELECT COUNT(*)::integer
     FROM public.finding_evidence AS evidence
     WHERE evidence.finding_id = summary.finding_id),
    COALESCE(summary.created_at, NOW()),
    COALESCE(summary.updated_at, NOW())
FROM public.finding_summaries AS summary
ON CONFLICT (id) DO NOTHING;

ALTER TABLE public.finding_evidence
    ADD COLUMN IF NOT EXISTS evidence_key TEXT,
    ADD COLUMN IF NOT EXISTS source_table TEXT,
    ADD COLUMN IF NOT EXISTS source_id TEXT,
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS weight REAL,
    ADD COLUMN IF NOT EXISTS excerpt_redacted TEXT,
    ADD COLUMN IF NOT EXISTS provenance JSONB;

-- Remove the legacy summary-table FK before attaching preserved orphan rows to
-- the sentinel finding. On rerun this also removes the canonical FK briefly;
-- it is recreated after every evidence row has a valid findings parent.
DO $drop_finding_evidence_fk$
DECLARE
    old_constraint RECORD;
BEGIN
    FOR old_constraint IN
        SELECT constraint_row.conname
        FROM pg_constraint AS constraint_row
        JOIN pg_attribute AS attribute_row
          ON attribute_row.attrelid = constraint_row.conrelid
         AND attribute_row.attnum = ANY (constraint_row.conkey)
        WHERE constraint_row.contype = 'f'
          AND constraint_row.conrelid = 'public.finding_evidence'::regclass
          AND attribute_row.attname = 'finding_id'
    LOOP
        EXECUTE format(
            'ALTER TABLE public.finding_evidence DROP CONSTRAINT %I',
            old_constraint.conname
        );
    END LOOP;
END
$drop_finding_evidence_fk$;

UPDATE public.finding_evidence
SET finding_id = '00000000-0000-0000-0000-000000000000'
WHERE finding_id IS NULL;

UPDATE public.finding_evidence
SET evidence_key = COALESCE(evidence_key, id::text),
    source_table = COALESCE(
        source_table,
        CASE WHEN message_id IS NULL THEN 'legacy_evidence' ELSE 'exfiltrated_messages' END
    ),
    source_id = COALESCE(source_id, message_id::text, id::text),
    observed_at = COALESCE(observed_at, last_seen_at, first_seen_at, NOW()),
    weight = COALESCE(weight, 1.0),
    provenance = COALESCE(provenance, '{}'::jsonb);

ALTER TABLE public.finding_evidence
    ALTER COLUMN finding_id SET NOT NULL,
    ALTER COLUMN evidence_key SET NOT NULL,
    ALTER COLUMN source_table SET NOT NULL,
    ALTER COLUMN source_id SET NOT NULL,
    ALTER COLUMN observed_at SET NOT NULL,
    ALTER COLUMN weight SET DEFAULT 1.0,
    ALTER COLUMN weight SET NOT NULL,
    ALTER COLUMN provenance SET DEFAULT '{}'::jsonb,
    ALTER COLUMN provenance SET NOT NULL;

DO $replace_finding_evidence_fk$
BEGIN
    ALTER TABLE public.finding_evidence
        ADD CONSTRAINT finding_evidence_finding_id_findings_fkey
        FOREIGN KEY (finding_id) REFERENCES public.findings(id) ON DELETE CASCADE;
END
$replace_finding_evidence_fk$;

DO $finding_evidence_checks$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.finding_evidence'::regclass
          AND conname = 'finding_evidence_weight_range'
    ) THEN
        ALTER TABLE public.finding_evidence
            ADD CONSTRAINT finding_evidence_weight_range
            CHECK (weight BETWEEN 0.0 AND 1.0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.finding_evidence'::regclass
          AND conname = 'finding_evidence_excerpt_limit'
    ) THEN
        ALTER TABLE public.finding_evidence
            ADD CONSTRAINT finding_evidence_excerpt_limit
            CHECK (excerpt_redacted IS NULL OR length(excerpt_redacted) <= 1000);
    END IF;
END
$finding_evidence_checks$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_finding_evidence_key
    ON public.finding_evidence(finding_id, evidence_key);
CREATE INDEX IF NOT EXISTS idx_finding_evidence_source
    ON public.finding_evidence(source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_finding_evidence_observed
    ON public.finding_evidence(observed_at DESC);

CREATE TABLE IF NOT EXISTS public.finding_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id UUID NOT NULL REFERENCES public.findings(id) ON DELETE CASCADE,
    actor_id UUID NOT NULL DEFAULT auth.uid(),
    label TEXT NOT NULL CHECK (label IN ('useful', 'noise', 'duplicate', 'irrelevant', 'actioned')),
    reason_code TEXT CHECK (reason_code IS NULL OR reason_code IN (
        'confirmed', 'actionable', 'false_positive', 'duplicate', 'out_of_scope', 'insufficient_evidence'
    )),
    note TEXT CHECK (note IS NULL OR length(note) <= 4000),
    status_after TEXT CHECK (status_after IS NULL OR status_after IN (
        'new', 'triaged', 'in_progress', 'resolved', 'dismissed', 'suppressed'
    )),
    assignee_after TEXT,
    suppress_pattern TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_finding_feedback_finding_created
    ON public.finding_feedback(finding_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_finding_feedback_actor_created
    ON public.finding_feedback(actor_id, created_at DESC);

CREATE OR REPLACE FUNCTION public.set_finding_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    NEW.updated_at := NOW();
    IF ROW(
        NEW.title, NEW.summary, NEW.confidence, NEW.severity,
        NEW.priority, NEW.score_explanation, NEW.evidence_count
    ) IS DISTINCT FROM ROW(
        OLD.title, OLD.summary, OLD.confidence, OLD.severity,
        OLD.priority, OLD.score_explanation, OLD.evidence_count
    ) THEN
        NEW.material_version := OLD.material_version + 1;
        NEW.last_material_change_at := NOW();
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS findings_set_updated_at ON public.findings;
CREATE TRIGGER findings_set_updated_at
BEFORE UPDATE ON public.findings
FOR EACH ROW EXECUTE FUNCTION public.set_finding_updated_at();

CREATE OR REPLACE FUNCTION public.recount_finding_evidence()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    IF TG_OP IN ('DELETE', 'UPDATE') THEN
        UPDATE public.findings
        SET evidence_count = (
            SELECT COUNT(*) FROM public.finding_evidence
            WHERE finding_id = OLD.finding_id
        )
        WHERE id = OLD.finding_id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        UPDATE public.findings
        SET evidence_count = (
            SELECT COUNT(*) FROM public.finding_evidence
            WHERE finding_id = NEW.finding_id
        )
        WHERE id = NEW.finding_id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS finding_evidence_recount ON public.finding_evidence;
CREATE TRIGGER finding_evidence_recount
AFTER INSERT OR DELETE OR UPDATE OF finding_id ON public.finding_evidence
FOR EACH ROW EXECUTE FUNCTION public.recount_finding_evidence();

CREATE OR REPLACE FUNCTION public.upsert_finding(
    p_type TEXT,
    p_canonical_key TEXT,
    p_title TEXT,
    p_summary TEXT,
    p_why_it_matters TEXT,
    p_recommended_action TEXT,
    p_confidence REAL,
    p_severity TEXT,
    p_priority INTEGER,
    p_score_explanation JSONB,
    p_first_seen_at TIMESTAMPTZ,
    p_last_seen_at TIMESTAMPTZ,
    p_evidence JSONB DEFAULT '[]'::jsonb
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    finding_uuid UUID;
BEGIN
    IF jsonb_typeof(p_evidence) <> 'array' THEN
        RAISE EXCEPTION 'p_evidence must be a JSON array';
    END IF;

    INSERT INTO public.findings (
        type, canonical_key, title, summary, why_it_matters,
        recommended_action, confidence, severity, priority,
        score_explanation, first_seen_at, last_seen_at
    ) VALUES (
        p_type, p_canonical_key, p_title, p_summary, p_why_it_matters,
        p_recommended_action, p_confidence, p_severity, p_priority,
        p_score_explanation, p_first_seen_at, p_last_seen_at
    )
    ON CONFLICT (type, canonical_key) DO UPDATE SET
        title = EXCLUDED.title,
        summary = EXCLUDED.summary,
        why_it_matters = EXCLUDED.why_it_matters,
        recommended_action = EXCLUDED.recommended_action,
        confidence = EXCLUDED.confidence,
        severity = EXCLUDED.severity,
        priority = EXCLUDED.priority,
        score_explanation = EXCLUDED.score_explanation,
        first_seen_at = LEAST(public.findings.first_seen_at, EXCLUDED.first_seen_at),
        last_seen_at = GREATEST(public.findings.last_seen_at, EXCLUDED.last_seen_at)
    RETURNING id INTO finding_uuid;

    INSERT INTO public.finding_evidence (
        finding_id, evidence_key, evidence_type, evidence_hash,
        source_table, source_id, observed_at, weight,
        excerpt_redacted, provenance, first_seen_at, last_seen_at
    )
    SELECT
        finding_uuid,
        evidence.value->>'evidence_key',
        evidence.value->>'evidence_type',
        md5(evidence.value->>'evidence_key'),
        evidence.value->>'source_table',
        evidence.value->>'source_id',
        COALESCE((evidence.value->>'observed_at')::timestamptz, p_last_seen_at),
        COALESCE((evidence.value->>'weight')::real, 1.0),
        evidence.value->>'excerpt_redacted',
        COALESCE(evidence.value->'provenance', '{}'::jsonb),
        COALESCE((evidence.value->>'observed_at')::timestamptz, p_first_seen_at),
        COALESCE((evidence.value->>'observed_at')::timestamptz, p_last_seen_at)
    FROM jsonb_array_elements(p_evidence) AS evidence(value)
    WHERE btrim(COALESCE(evidence.value->>'evidence_key', '')) <> ''
      AND btrim(COALESCE(evidence.value->>'evidence_type', '')) <> ''
      AND btrim(COALESCE(evidence.value->>'source_table', '')) <> ''
      AND btrim(COALESCE(evidence.value->>'source_id', '')) <> ''
    ON CONFLICT (finding_id, evidence_key) DO UPDATE SET
        observed_at = GREATEST(public.finding_evidence.observed_at, EXCLUDED.observed_at),
        last_seen_at = GREATEST(public.finding_evidence.last_seen_at, EXCLUDED.last_seen_at),
        weight = EXCLUDED.weight,
        excerpt_redacted = EXCLUDED.excerpt_redacted,
        provenance = public.finding_evidence.provenance || EXCLUDED.provenance;

    RETURN finding_uuid;
END
$function$;

CREATE OR REPLACE FUNCTION public.record_finding_feedback(
    p_finding_id UUID,
    p_label TEXT,
    p_reason_code TEXT DEFAULT NULL,
    p_note TEXT DEFAULT NULL,
    p_status TEXT DEFAULT NULL,
    p_assignee TEXT DEFAULT NULL,
    p_suppress_pattern TEXT DEFAULT NULL
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    feedback_uuid UUID;
    acting_user UUID := auth.uid();
BEGIN
    IF acting_user IS NULL THEN
        RAISE EXCEPTION 'Authentication required';
    END IF;

    INSERT INTO public.finding_feedback (
        finding_id, actor_id, label, reason_code, note,
        status_after, assignee_after, suppress_pattern
    ) VALUES (
        p_finding_id, acting_user, p_label, p_reason_code, p_note,
        p_status, p_assignee, p_suppress_pattern
    )
    RETURNING id INTO feedback_uuid;

    UPDATE public.findings
    SET status = COALESCE(p_status, status),
        assignee = COALESCE(p_assignee, assignee)
    WHERE id = p_finding_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Finding not found';
    END IF;

    INSERT INTO public.audit_logs (event_type, user_agent, success, details)
    VALUES (
        'finding.feedback',
        acting_user::text,
        TRUE,
        jsonb_build_object(
            'finding_id', p_finding_id,
            'feedback_id', feedback_uuid,
            'label', p_label,
            'reason_code', p_reason_code,
            'status_after', p_status
        )
    );

    RETURN feedback_uuid;
END
$function$;

CREATE OR REPLACE FUNCTION public.upsert_findings_batch(p_candidates JSONB)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    candidate JSONB;
    candidate_count INTEGER := 0;
BEGIN
    IF jsonb_typeof(p_candidates) <> 'array' THEN
        RAISE EXCEPTION 'p_candidates must be a JSON array';
    END IF;
    IF jsonb_array_length(p_candidates) > 250 THEN
        RAISE EXCEPTION 'p_candidates is limited to 250 findings per call';
    END IF;

    FOR candidate IN SELECT value FROM jsonb_array_elements(p_candidates)
    LOOP
        PERFORM public.upsert_finding(
            candidate->>'p_type',
            candidate->>'p_canonical_key',
            candidate->>'p_title',
            candidate->>'p_summary',
            candidate->>'p_why_it_matters',
            candidate->>'p_recommended_action',
            (candidate->>'p_confidence')::real,
            candidate->>'p_severity',
            (candidate->>'p_priority')::integer,
            candidate->'p_score_explanation',
            (candidate->>'p_first_seen_at')::timestamptz,
            (candidate->>'p_last_seen_at')::timestamptz,
            COALESCE(candidate->'p_evidence', '[]'::jsonb)
        );
        candidate_count := candidate_count + 1;
    END LOOP;

    RETURN candidate_count;
END
$function$;

ALTER TABLE public.findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finding_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finding_feedback ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS findings_authenticated_read ON public.findings;
CREATE POLICY findings_authenticated_read ON public.findings
    FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS finding_evidence_authenticated_read ON public.finding_evidence;
CREATE POLICY finding_evidence_authenticated_read ON public.finding_evidence
    FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS finding_feedback_authenticated_read ON public.finding_feedback;
CREATE POLICY finding_feedback_authenticated_read ON public.finding_feedback
    FOR SELECT TO authenticated USING (true);

REVOKE ALL ON public.findings FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.finding_evidence FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.finding_feedback FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.findings TO authenticated;
GRANT SELECT ON public.finding_evidence TO authenticated;
GRANT SELECT ON public.finding_feedback TO authenticated;
GRANT ALL ON public.findings TO service_role;
GRANT ALL ON public.finding_evidence TO service_role;
GRANT ALL ON public.finding_feedback TO service_role;

REVOKE ALL ON FUNCTION public.upsert_finding(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, REAL, TEXT, INTEGER,
    JSONB, TIMESTAMPTZ, TIMESTAMPTZ, JSONB
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_finding(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, REAL, TEXT, INTEGER,
    JSONB, TIMESTAMPTZ, TIMESTAMPTZ, JSONB
) TO service_role;

REVOKE ALL ON FUNCTION public.upsert_findings_batch(JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_findings_batch(JSONB)
    TO service_role;

REVOKE ALL ON FUNCTION public.record_finding_feedback(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.record_finding_feedback(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) TO authenticated, service_role;

COMMENT ON TABLE public.findings IS
    'Persistent prioritized Insight Queue. Exactly three finding types in v1.';
COMMENT ON TABLE public.finding_evidence IS
    'Redacted provenance links from a finding to durable source evidence.';
COMMENT ON TABLE public.finding_feedback IS
    'Append-only authenticated analyst labels, reasons, notes, and dispositions.';


-- =====================================================================
-- SOURCE: 20260904000003_entities_engagement.sql
-- =====================================================================
-- Plan Items 4/6: typed evidence graph and owned-bot voluntary funnel.

CREATE TABLE IF NOT EXISTS public.entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'credential', 'bot', 'webhook_host', 'domain', 'url',
        'wallet', 'media_hash', 'user_pseudonym'
    )),
    canonical_value TEXT NOT NULL CHECK (btrim(canonical_value) <> ''),
    display_value_redacted TEXT NOT NULL CHECK (btrim(display_value_redacted) <> ''),
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(provenance) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT entities_seen_order CHECK (last_seen_at >= first_seen_at),
    CONSTRAINT entities_type_value_unique UNIQUE (entity_type, canonical_value)
);

CREATE INDEX IF NOT EXISTS idx_entities_recent
    ON public.entities(entity_type, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_entities_confidence
    ON public.entities(confidence DESC, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS public.entity_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_key TEXT NOT NULL UNIQUE CHECK (btrim(edge_key) <> ''),
    source_entity_id UUID NOT NULL REFERENCES public.entities(id) ON DELETE RESTRICT,
    target_entity_id UUID NOT NULL REFERENCES public.entities(id) ON DELETE RESTRICT,
    edge_type TEXT NOT NULL CHECK (edge_type IN (
        'represents_bot', 'uses_infrastructure', 'observed_indicator',
        'shares_media', 'interacted_with'
    )),
    evidence_source_table TEXT NOT NULL CHECK (btrim(evidence_source_table) <> ''),
    evidence_source_id TEXT NOT NULL CHECK (btrim(evidence_source_id) <> ''),
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(provenance) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT entity_edges_distinct_nodes CHECK (source_entity_id <> target_entity_id),
    CONSTRAINT entity_edges_seen_order CHECK (last_seen_at >= first_seen_at)
);

CREATE INDEX IF NOT EXISTS idx_entity_edges_source
    ON public.entity_edges(source_entity_id, edge_type, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_entity_edges_target
    ON public.entity_edges(target_entity_id, edge_type, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_entity_edges_evidence
    ON public.entity_edges(evidence_source_table, evidence_source_id);

CREATE OR REPLACE FUNCTION public.upsert_entity_edge(
    p_edge_key TEXT,
    p_source JSONB,
    p_target JSONB,
    p_edge_type TEXT,
    p_evidence_source_table TEXT,
    p_evidence_source_id TEXT,
    p_first_seen_at TIMESTAMPTZ,
    p_last_seen_at TIMESTAMPTZ,
    p_confidence REAL,
    p_provenance JSONB DEFAULT '{}'::jsonb
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    source_uuid UUID;
    target_uuid UUID;
    edge_uuid UUID;
BEGIN
    IF jsonb_typeof(p_source) <> 'object' OR jsonb_typeof(p_target) <> 'object' THEN
        RAISE EXCEPTION 'p_source and p_target must be JSON objects';
    END IF;

    INSERT INTO public.entities (
        entity_type, canonical_value, display_value_redacted,
        first_seen_at, last_seen_at, confidence, provenance
    ) VALUES (
        p_source->>'entity_type',
        p_source->>'canonical_value',
        p_source->>'display_value_redacted',
        COALESCE((p_source->>'first_seen_at')::timestamptz, p_first_seen_at),
        COALESCE((p_source->>'last_seen_at')::timestamptz, p_last_seen_at),
        COALESCE((p_source->>'confidence')::real, p_confidence),
        COALESCE(p_source->'provenance', '{}'::jsonb)
    )
    ON CONFLICT (entity_type, canonical_value) DO UPDATE SET
        display_value_redacted = EXCLUDED.display_value_redacted,
        first_seen_at = LEAST(public.entities.first_seen_at, EXCLUDED.first_seen_at),
        last_seen_at = GREATEST(public.entities.last_seen_at, EXCLUDED.last_seen_at),
        confidence = GREATEST(public.entities.confidence, EXCLUDED.confidence),
        provenance = public.entities.provenance || EXCLUDED.provenance,
        updated_at = NOW()
    RETURNING id INTO source_uuid;

    INSERT INTO public.entities (
        entity_type, canonical_value, display_value_redacted,
        first_seen_at, last_seen_at, confidence, provenance
    ) VALUES (
        p_target->>'entity_type',
        p_target->>'canonical_value',
        p_target->>'display_value_redacted',
        COALESCE((p_target->>'first_seen_at')::timestamptz, p_first_seen_at),
        COALESCE((p_target->>'last_seen_at')::timestamptz, p_last_seen_at),
        COALESCE((p_target->>'confidence')::real, p_confidence),
        COALESCE(p_target->'provenance', '{}'::jsonb)
    )
    ON CONFLICT (entity_type, canonical_value) DO UPDATE SET
        display_value_redacted = EXCLUDED.display_value_redacted,
        first_seen_at = LEAST(public.entities.first_seen_at, EXCLUDED.first_seen_at),
        last_seen_at = GREATEST(public.entities.last_seen_at, EXCLUDED.last_seen_at),
        confidence = GREATEST(public.entities.confidence, EXCLUDED.confidence),
        provenance = public.entities.provenance || EXCLUDED.provenance,
        updated_at = NOW()
    RETURNING id INTO target_uuid;

    INSERT INTO public.entity_edges (
        edge_key, source_entity_id, target_entity_id, edge_type,
        evidence_source_table, evidence_source_id, first_seen_at,
        last_seen_at, confidence, provenance
    ) VALUES (
        p_edge_key, source_uuid, target_uuid, p_edge_type,
        p_evidence_source_table, p_evidence_source_id, p_first_seen_at,
        p_last_seen_at, p_confidence, COALESCE(p_provenance, '{}'::jsonb)
    )
    ON CONFLICT (edge_key) DO UPDATE SET
        first_seen_at = LEAST(public.entity_edges.first_seen_at, EXCLUDED.first_seen_at),
        last_seen_at = GREATEST(public.entity_edges.last_seen_at, EXCLUDED.last_seen_at),
        confidence = GREATEST(public.entity_edges.confidence, EXCLUDED.confidence),
        provenance = public.entity_edges.provenance || EXCLUDED.provenance,
        updated_at = NOW()
    RETURNING id INTO edge_uuid;

    RETURN edge_uuid;
END
$function$;

CREATE OR REPLACE FUNCTION public.upsert_entity_edges_batch(p_edges JSONB)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    edge JSONB;
    edge_count INTEGER := 0;
BEGIN
    IF jsonb_typeof(p_edges) <> 'array' THEN
        RAISE EXCEPTION 'p_edges must be a JSON array';
    END IF;
    IF jsonb_array_length(p_edges) > 500 THEN
        RAISE EXCEPTION 'p_edges is limited to 500 edges per call';
    END IF;

    FOR edge IN SELECT value FROM jsonb_array_elements(p_edges)
    LOOP
        PERFORM public.upsert_entity_edge(
            edge->>'p_edge_key',
            edge->'p_source',
            edge->'p_target',
            edge->>'p_edge_type',
            edge->>'p_evidence_source_table',
            edge->>'p_evidence_source_id',
            (edge->>'p_first_seen_at')::timestamptz,
            (edge->>'p_last_seen_at')::timestamptz,
            (edge->>'p_confidence')::real,
            COALESCE(edge->'p_provenance', '{}'::jsonb)
        );
        edge_count := edge_count + 1;
    END LOOP;
    RETURN edge_count;
END
$function$;

CREATE TABLE IF NOT EXISTS public.engagement_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_key TEXT NOT NULL UNIQUE CHECK (btrim(event_key) <> ''),
    owned_bot_id BIGINT NOT NULL,
    subject_pseudonym TEXT NOT NULL CHECK (btrim(subject_pseudonym) <> ''),
    campaign_id TEXT NOT NULL CHECK (campaign_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    campaign_source TEXT NOT NULL CHECK (campaign_source ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'start', 'first_inbound', 'qualified', 'handoff', 'outcome',
        'opt_out', 'block_report'
    )),
    occurred_at TIMESTAMPTZ NOT NULL,
    last_occurred_at TIMESTAMPTZ NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    metadata_redacted JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata_redacted) = 'object'),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT engagement_event_time_order CHECK (last_occurred_at >= occurred_at),
    CONSTRAINT engagement_event_expiry_order CHECK (expires_at >= occurred_at)
);

CREATE INDEX IF NOT EXISTS idx_engagement_events_funnel
    ON public.engagement_events(campaign_id, campaign_source, event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_engagement_events_subject
    ON public.engagement_events(owned_bot_id, subject_pseudonym, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_engagement_events_expiry
    ON public.engagement_events(expires_at);

CREATE OR REPLACE FUNCTION public.upsert_engagement_event(
    p_event_key TEXT,
    p_owned_bot_id BIGINT,
    p_subject_pseudonym TEXT,
    p_campaign_id TEXT,
    p_campaign_source TEXT,
    p_event_type TEXT,
    p_occurred_at TIMESTAMPTZ,
    p_metadata_redacted JSONB DEFAULT '{}'::jsonb
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    event_uuid UUID;
BEGIN
    INSERT INTO public.engagement_events (
        event_key, owned_bot_id, subject_pseudonym, campaign_id,
        campaign_source, event_type, occurred_at, last_occurred_at,
        metadata_redacted, expires_at
    ) VALUES (
        p_event_key, p_owned_bot_id, p_subject_pseudonym, p_campaign_id,
        p_campaign_source, p_event_type, p_occurred_at, p_occurred_at,
        COALESCE(p_metadata_redacted, '{}'::jsonb),
        p_occurred_at + INTERVAL '180 days'
    )
    ON CONFLICT (event_key) DO UPDATE SET
        last_occurred_at = GREATEST(public.engagement_events.last_occurred_at, EXCLUDED.last_occurred_at),
        metadata_redacted = public.engagement_events.metadata_redacted || EXCLUDED.metadata_redacted,
        expires_at = GREATEST(public.engagement_events.expires_at, EXCLUDED.expires_at),
        updated_at = NOW()
    RETURNING id INTO event_uuid;
    RETURN event_uuid;
END
$function$;

CREATE OR REPLACE VIEW public.engagement_funnel_daily
WITH (security_invoker = true) AS
SELECT
    date_trunc('day', occurred_at) AS day,
    owned_bot_id,
    campaign_id,
    campaign_source,
    COUNT(*) FILTER (WHERE event_type = 'start') AS starts,
    COUNT(*) FILTER (WHERE event_type = 'first_inbound') AS first_inbounds,
    COUNT(*) FILTER (WHERE event_type = 'qualified') AS qualified,
    COUNT(*) FILTER (WHERE event_type = 'handoff') AS handoffs,
    COUNT(*) FILTER (WHERE event_type = 'outcome') AS outcomes,
    COUNT(*) FILTER (WHERE event_type = 'opt_out') AS opt_outs,
    COUNT(*) FILTER (WHERE event_type = 'block_report') AS block_reports
FROM public.engagement_events
GROUP BY date_trunc('day', occurred_at), owned_bot_id, campaign_id, campaign_source;

ALTER TABLE public.entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.entity_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.engagement_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS entities_authenticated_read ON public.entities;
CREATE POLICY entities_authenticated_read ON public.entities
    FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS entity_edges_authenticated_read ON public.entity_edges;
CREATE POLICY entity_edges_authenticated_read ON public.entity_edges
    FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS engagement_events_authenticated_read ON public.engagement_events;
CREATE POLICY engagement_events_authenticated_read ON public.engagement_events
    FOR SELECT TO authenticated USING (true);

REVOKE ALL ON public.entities FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.entity_edges FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.engagement_events FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.engagement_funnel_daily FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.entities TO authenticated;
GRANT SELECT ON public.entity_edges TO authenticated;
GRANT SELECT ON public.engagement_events TO authenticated;
GRANT SELECT ON public.engagement_funnel_daily TO authenticated;
GRANT ALL ON public.entities TO service_role;
GRANT ALL ON public.entity_edges TO service_role;
GRANT ALL ON public.engagement_events TO service_role;
GRANT SELECT ON public.engagement_funnel_daily TO service_role;

REVOKE ALL ON FUNCTION public.upsert_entity_edge(
    TEXT, JSONB, JSONB, TEXT, TEXT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, REAL, JSONB
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_entity_edge(
    TEXT, JSONB, JSONB, TEXT, TEXT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, REAL, JSONB
) TO service_role;
REVOKE ALL ON FUNCTION public.upsert_entity_edges_batch(JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_entity_edges_batch(JSONB)
    TO service_role;
REVOKE ALL ON FUNCTION public.upsert_engagement_event(
    TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ, JSONB
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_engagement_event(
    TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ, JSONB
) TO service_role;

COMMENT ON TABLE public.entities IS
    'Typed canonical evidence nodes with redacted display values and provenance.';
COMMENT ON TABLE public.entity_edges IS
    'Evidence-backed typed relationships. Shared infrastructure is correlation, not attribution.';
COMMENT ON TABLE public.engagement_events IS
    'Pseudonymous 180-day events from voluntary interactions with monitor bots owned by this deployment.';


-- =====================================================================
-- SOURCE: 20260904000004_finding_alert_policies.sql
-- =====================================================================
-- Plan Items 3/7: policy-routed material deltas, digests, and delivery audit.

CREATE TABLE IF NOT EXISTS public.finding_alert_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE CHECK (btrim(name) <> ''),
    finding_type TEXT CHECK (finding_type IS NULL OR finding_type IN (
        'credential_exposure', 'infrastructure_cluster', 'cross_bot_pattern'
    )),
    min_priority SMALLINT NOT NULL DEFAULT 1 CHECK (min_priority BETWEEN 1 AND 10),
    monitored_entity_type TEXT CHECK (
        monitored_entity_type IS NULL OR monitored_entity_type IN (
            'credential', 'bot', 'webhook_host', 'domain', 'url',
            'wallet', 'media_hash', 'user_pseudonym'
        )
    ),
    monitored_entity_value TEXT,
    cadence TEXT NOT NULL CHECK (cadence IN ('immediate', 'daily', 'weekly')),
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'webhook')),
    timezone TEXT NOT NULL DEFAULT 'UTC' CHECK (btrim(timezone) <> ''),
    quiet_start TIME,
    quiet_end TIME,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by UUID DEFAULT auth.uid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT finding_alert_policy_entity_pair CHECK (
        (monitored_entity_type IS NULL AND monitored_entity_value IS NULL)
        OR (
            monitored_entity_type IS NOT NULL
            AND btrim(COALESCE(monitored_entity_value, '')) <> ''
        )
    ),
    CONSTRAINT finding_alert_policy_quiet_pair CHECK (
        (quiet_start IS NULL) = (quiet_end IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_finding_alert_policies_route
    ON public.finding_alert_policies(enabled, cadence, channel, min_priority);

CREATE TABLE IF NOT EXISTS public.finding_alert_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id UUID NOT NULL REFERENCES public.finding_alert_policies(id) ON DELETE RESTRICT,
    finding_id UUID NOT NULL REFERENCES public.findings(id) ON DELETE CASCADE,
    material_version INTEGER NOT NULL CHECK (material_version >= 1),
    cadence TEXT NOT NULL CHECK (cadence IN ('immediate', 'daily', 'weekly')),
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'webhook')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'deferred', 'delivered', 'failed')),
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    payload_redacted JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload_redacted) = 'object'),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claim_until TIMESTAMPTZ,
    first_evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT finding_alert_delivery_once UNIQUE (
        policy_id, finding_id, material_version
    )
);

CREATE INDEX IF NOT EXISTS idx_finding_alert_deliveries_status
    ON public.finding_alert_deliveries(status, last_evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_finding_alert_deliveries_finding
    ON public.finding_alert_deliveries(finding_id, material_version DESC);

CREATE TABLE IF NOT EXISTS public.finding_alert_audit (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    delivery_id UUID NOT NULL
        REFERENCES public.finding_alert_deliveries(id) ON DELETE CASCADE,
    policy_id UUID NOT NULL,
    finding_id UUID NOT NULL,
    material_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_finding_alert_audit_recorded
    ON public.finding_alert_audit(recorded_at DESC, status);

CREATE OR REPLACE FUNCTION public.set_finding_alert_policy_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS finding_alert_policy_updated_at
    ON public.finding_alert_policies;
CREATE TRIGGER finding_alert_policy_updated_at
BEFORE UPDATE ON public.finding_alert_policies
FOR EACH ROW EXECUTE FUNCTION public.set_finding_alert_policy_updated_at();

CREATE OR REPLACE FUNCTION public.audit_finding_alert_delivery()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    IF TG_OP = 'INSERT'
       OR ROW(NEW.status, NEW.reason) IS DISTINCT FROM ROW(OLD.status, OLD.reason)
    THEN
        INSERT INTO public.finding_alert_audit (
            delivery_id, policy_id, finding_id, material_version, status, reason
        ) VALUES (
            NEW.id, NEW.policy_id, NEW.finding_id,
            NEW.material_version, NEW.status, NEW.reason
        );
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS finding_alert_delivery_audit
    ON public.finding_alert_deliveries;
CREATE TRIGGER finding_alert_delivery_audit
AFTER INSERT OR UPDATE ON public.finding_alert_deliveries
FOR EACH ROW EXECUTE FUNCTION public.audit_finding_alert_delivery();

CREATE OR REPLACE FUNCTION public.claim_finding_alert(
    p_policy_id UUID,
    p_finding_id UUID,
    p_material_version INTEGER,
    p_cadence TEXT,
    p_channel TEXT,
    p_payload_redacted JSONB,
    p_evaluated_at TIMESTAMPTZ
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    delivery_uuid UUID;
BEGIN
    INSERT INTO public.finding_alert_deliveries (
        policy_id, finding_id, material_version, cadence, channel,
        status, reason, payload_redacted, attempt_count, claim_until,
        first_evaluated_at, last_evaluated_at
    ) VALUES (
        p_policy_id, p_finding_id, p_material_version, p_cadence, p_channel,
        'pending', 'claimed', COALESCE(p_payload_redacted, '{}'::jsonb), 1,
        p_evaluated_at + INTERVAL '10 minutes', p_evaluated_at, p_evaluated_at
    )
    ON CONFLICT (policy_id, finding_id, material_version) DO UPDATE SET
        cadence = EXCLUDED.cadence,
        channel = EXCLUDED.channel,
        status = 'pending',
        reason = 'reclaimed',
        payload_redacted = EXCLUDED.payload_redacted,
        attempt_count = public.finding_alert_deliveries.attempt_count + 1,
        claim_until = EXCLUDED.claim_until,
        last_evaluated_at = EXCLUDED.last_evaluated_at,
        updated_at = NOW()
    WHERE public.finding_alert_deliveries.status IN ('deferred', 'failed')
       OR (
            public.finding_alert_deliveries.status = 'pending'
            AND public.finding_alert_deliveries.claim_until < p_evaluated_at
       )
    RETURNING id INTO delivery_uuid;

    RETURN delivery_uuid;
END
$function$;

CREATE OR REPLACE FUNCTION public.defer_finding_alert(
    p_policy_id UUID,
    p_finding_id UUID,
    p_material_version INTEGER,
    p_cadence TEXT,
    p_channel TEXT,
    p_reason TEXT,
    p_payload_redacted JSONB,
    p_evaluated_at TIMESTAMPTZ
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    delivery_uuid UUID;
BEGIN
    INSERT INTO public.finding_alert_deliveries (
        policy_id, finding_id, material_version, cadence, channel,
        status, reason, payload_redacted, first_evaluated_at, last_evaluated_at
    ) VALUES (
        p_policy_id, p_finding_id, p_material_version, p_cadence, p_channel,
        'deferred', p_reason, COALESCE(p_payload_redacted, '{}'::jsonb),
        p_evaluated_at, p_evaluated_at
    )
    ON CONFLICT (policy_id, finding_id, material_version) DO UPDATE SET
        status = 'deferred',
        reason = EXCLUDED.reason,
        payload_redacted = EXCLUDED.payload_redacted,
        last_evaluated_at = EXCLUDED.last_evaluated_at,
        updated_at = NOW()
    WHERE public.finding_alert_deliveries.status <> 'delivered'
      AND (
            public.finding_alert_deliveries.status <> 'pending'
            OR public.finding_alert_deliveries.claim_until < p_evaluated_at
      )
    RETURNING id INTO delivery_uuid;

    RETURN delivery_uuid;
END
$function$;

CREATE OR REPLACE FUNCTION public.complete_finding_alert(
    p_delivery_id UUID,
    p_success BOOLEAN,
    p_reason TEXT,
    p_completed_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
BEGIN
    UPDATE public.finding_alert_deliveries
    SET status = CASE WHEN p_success THEN 'delivered' ELSE 'failed' END,
        reason = p_reason,
        claim_until = NULL,
        delivered_at = CASE WHEN p_success THEN p_completed_at ELSE NULL END,
        last_evaluated_at = p_completed_at,
        updated_at = NOW()
    WHERE id = p_delivery_id
      AND status = 'pending';
    RETURN FOUND;
END
$function$;

-- Default behavior: only high-priority material deltas alert immediately;
-- the separate daily policy produces a bounded Top Findings digest.
INSERT INTO public.finding_alert_policies (
    id, name, min_priority, cadence, channel, timezone, quiet_start, quiet_end
) VALUES
    (
        '10000000-0000-0000-0000-000000000001',
        'Default high-priority material deltas',
        8, 'immediate', 'telegram', 'UTC', '22:00', '07:00'
    ),
    (
        '10000000-0000-0000-0000-000000000002',
        'Daily Top Findings',
        1, 'daily', 'telegram', 'UTC', NULL, NULL
    )
ON CONFLICT DO NOTHING;

ALTER TABLE public.finding_alert_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finding_alert_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finding_alert_audit ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS finding_alert_policies_authenticated_read
    ON public.finding_alert_policies;
CREATE POLICY finding_alert_policies_authenticated_read
    ON public.finding_alert_policies FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS finding_alert_deliveries_authenticated_read
    ON public.finding_alert_deliveries;
CREATE POLICY finding_alert_deliveries_authenticated_read
    ON public.finding_alert_deliveries FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS finding_alert_audit_authenticated_read
    ON public.finding_alert_audit;
CREATE POLICY finding_alert_audit_authenticated_read
    ON public.finding_alert_audit FOR SELECT TO authenticated USING (true);

REVOKE ALL ON public.finding_alert_policies FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.finding_alert_deliveries FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.finding_alert_audit FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.finding_alert_policies TO authenticated;
GRANT SELECT ON public.finding_alert_deliveries TO authenticated;
GRANT SELECT ON public.finding_alert_audit TO authenticated;
GRANT ALL ON public.finding_alert_policies TO service_role;
GRANT ALL ON public.finding_alert_deliveries TO service_role;
GRANT ALL ON public.finding_alert_audit TO service_role;

REVOKE ALL ON FUNCTION public.claim_finding_alert(
    UUID, UUID, INTEGER, TEXT, TEXT, JSONB, TIMESTAMPTZ
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_finding_alert(
    UUID, UUID, INTEGER, TEXT, TEXT, JSONB, TIMESTAMPTZ
) TO service_role;
REVOKE ALL ON FUNCTION public.defer_finding_alert(
    UUID, UUID, INTEGER, TEXT, TEXT, TEXT, JSONB, TIMESTAMPTZ
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.defer_finding_alert(
    UUID, UUID, INTEGER, TEXT, TEXT, TEXT, JSONB, TIMESTAMPTZ
) TO service_role;
REVOKE ALL ON FUNCTION public.complete_finding_alert(
    UUID, BOOLEAN, TEXT, TIMESTAMPTZ
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.complete_finding_alert(
    UUID, BOOLEAN, TEXT, TIMESTAMPTZ
) TO service_role;

COMMENT ON TABLE public.finding_alert_policies IS
    'Operator alert routing by finding type, priority, entity, cadence, channel, timezone, and quiet hours.';
COMMENT ON TABLE public.finding_alert_deliveries IS
    'Idempotent claim/delivery state for each policy and material finding version.';
COMMENT ON TABLE public.finding_alert_audit IS
    'Append-only status transitions used for weekly alert coverage and suppression review.';


-- =====================================================================
-- SOURCE: 20260904000005_monitor_findings_feedback.sql
-- =====================================================================
-- Plan Item 8: transactional feedback from monitor-key API clients.
-- Browser users continue to use record_finding_feedback(), which binds the
-- actor to auth.uid(). This separate function is service-role-only and accepts
-- the pseudonymous actor UUID derived by the monitor-key dependency.

CREATE OR REPLACE FUNCTION public.record_finding_feedback_service(
    p_actor_id UUID,
    p_finding_id UUID,
    p_label TEXT,
    p_reason_code TEXT DEFAULT NULL,
    p_note TEXT DEFAULT NULL,
    p_status TEXT DEFAULT NULL,
    p_assignee TEXT DEFAULT NULL,
    p_suppress_pattern TEXT DEFAULT NULL
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    feedback_uuid UUID;
BEGIN
    IF p_actor_id IS NULL THEN
        RAISE EXCEPTION 'Actor is required';
    END IF;

    UPDATE public.findings
    SET status = COALESCE(p_status, status),
        assignee = COALESCE(p_assignee, assignee)
    WHERE id = p_finding_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Finding not found';
    END IF;

    INSERT INTO public.finding_feedback (
        finding_id, actor_id, label, reason_code, note,
        status_after, assignee_after, suppress_pattern
    ) VALUES (
        p_finding_id, p_actor_id, p_label, p_reason_code, p_note,
        p_status, p_assignee, p_suppress_pattern
    )
    RETURNING id INTO feedback_uuid;

    INSERT INTO public.audit_logs (event_type, user_agent, success, details)
    VALUES (
        'finding.feedback',
        'monitor_api:' || left(p_actor_id::text, 8),
        TRUE,
        jsonb_build_object(
            'finding_id', p_finding_id,
            'feedback_id', feedback_uuid,
            'label', p_label,
            'reason_code', p_reason_code,
            'status_after', p_status,
            'source', 'monitor_api'
        )
    );

    RETURN feedback_uuid;
END
$function$;

REVOKE ALL ON FUNCTION public.record_finding_feedback_service(
    UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_finding_feedback_service(
    UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) TO service_role;

COMMENT ON FUNCTION public.record_finding_feedback_service(
    UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) IS 'Service-only finding feedback with a pseudonymous monitor API actor.';


-- =====================================================================
-- SOURCE: 20260906000001_dashboard_operator_authorization.sql
-- =====================================================================
-- Dashboard operator authorization guard
-- Requires JWT app_metadata.operator claim for operator-level access

CREATE OR REPLACE FUNCTION public.is_dashboard_operator()
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
BEGIN
  -- Must have authenticated user with operator claim in app_metadata
  RETURN EXISTS (
    SELECT 1
    FROM auth.jwt() AS jwt
    WHERE jwt->'app_metadata'->>'operator' IS NOT NULL
  );
END;
$function$;

-- Guard the queue tables (findings, feedback, evidence)
ALTER TABLE public.findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finding_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evidence ENABLE ROW LEVEL SECURITY;

-- Findings: only operators can read/write
DROP POLICY IF EXISTS findings_authenticated_read ON public.findings;
CREATE POLICY findings_operator_only ON public.findings
  FOR ALL TO authenticated
  USING (public.is_dashboard_operator())
  WITH CHECK (public.is_dashboard_operator());

-- Finding feedback: only operators
DROP POLICY IF EXISTS finding_feedback_authenticated_read ON public.finding_feedback;
CREATE POLICY finding_feedback_operator_only ON public.finding_feedback
  FOR ALL TO authenticated
  USING (public.is_dashboard_operator())
  WITH CHECK (public.is_dashboard_operator());

-- Evidence: only operators
DROP POLICY IF EXISTS evidence_authenticated_read ON public.evidence;
CREATE POLICY evidence_operator_only ON public.evidence
  FOR ALL TO authenticated
  USING (public.is_dashboard_operator())
  WITH CHECK (public.is_dashboard_operator());

-- Guard the redacted dashboard views
-- findings_dashboard_redacted (if exists)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.views WHERE table_schema = 'public' AND table_name = 'findings_dashboard_redacted') THEN
    ALTER VIEW public.findings_dashboard_redacted SET (security_invoker = true);
  END IF;
END $$;

-- evidence_dashboard_redacted (if exists)  
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.views WHERE table_schema = 'public' AND table_name = 'evidence_dashboard_redacted') THEN
    ALTER VIEW public.evidence_dashboard_redacted SET (security_invoker = true);
  END IF;
END $$;

-- Guard record_finding_feedback RPC (if it uses auth.uid())
-- The function must check is_dashboard_operator() for sensitive operations

-- Grant execute on is_dashboard_operator to authenticated users
GRANT EXECUTE ON FUNCTION public.is_dashboard_operator() TO authenticated;

COMMENT ON FUNCTION public.is_dashboard_operator() IS 
'Returns true if the authenticated user has operator claim in JWT app_metadata. Guards dashboard operator surfaces.';


-- =====================================================================
-- SOURCE: 20260906000006_verify_pending_migrations.sql
-- =====================================================================
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
-- ROLLBACK
-- No DDL was performed; nothing to roll back. This migration is a check only.
-- =====================================================================


-- =====================================================================
-- SOURCE: 20260906000003_broadcast_reliability_ext.sql
-- =====================================================================
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


-- =====================================================================
-- SOURCE: 20260906000004_media_hashes_failure_flag.sql
-- =====================================================================
-- =====================================================================
-- Migration: 20260906000004_media_hashes_failure_flag.sql
-- Findings:  INTR-005 (fragile sentinel string), CONC-003 (dup-on-race)
-- Purpose:   Replace the sha256='__failed__<id>' sentinel scheme with a
--            structured is_failure/failure_reason pair, and enforce
--            UNIQUE(message_id) so concurrent hash_exfil_media runs cannot
--            insert duplicate rows for the same message.
-- Safety:    Additive columns + partial unique index + one UPDATE backfill.
--            Idempotent (IF NOT EXISTS guards on additions).
-- =====================================================================

ALTER TABLE public.media_hashes
    ADD COLUMN IF NOT EXISTS is_failure BOOLEAN DEFAULT FALSE;

ALTER TABLE public.media_hashes
    ADD COLUMN IF NOT EXISTS failure_reason TEXT;

-- Migrate existing sentinel rows: any row whose sha256 starts with
-- '__failed__' represents a failed download attempt. Preserve the sentinel
-- string in sha256 for now (existing dedup logic still keys off it) and mark
-- the new boolean so the code can transition cleanly to the flag.
UPDATE public.media_hashes
   SET is_failure = TRUE,
       failure_reason = COALESCE(failure_reason, error, 'legacy_sentinel')
 WHERE is_failure = FALSE
   AND sha256 LIKE '__failed__%';

-- CONC-003: enforce one row per message_id. Prior sentinel scheme guaranteed
-- uniqueness by baking msg_id[:8] into sha256, but two concurrent hash runs
-- could produce identical (real) sha256 rows for the same message_id.
--
-- Guard: only add the unique index if it isn't already present. Use partial
-- form so historical sentinel duplicates (if any) aren't caught retroactively.
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
-- ROLLBACK
-- =====================================================================
-- DROP INDEX IF EXISTS public.idx_media_hashes_is_failure;
-- DROP INDEX IF EXISTS public.idx_media_hashes_message_id_unique;
-- ALTER TABLE public.media_hashes DROP COLUMN IF EXISTS failure_reason;
-- ALTER TABLE public.media_hashes DROP COLUMN IF EXISTS is_failure;


-- =====================================================================
-- SOURCE: 20260906000005_audit_logs_composite_idx.sql
-- =====================================================================
-- =====================================================================
-- Migration: 20260906000005_audit_logs_composite_idx.sql
-- Finding:   PERF-001
-- Purpose:   Add a composite (event_type, timestamp DESC) index so
--            /health/operational and other queries that filter on both
--            columns can use a single index scan instead of a bitmap-heap
--            merge across two single-column indexes.
-- Safety:    CONCURRENTLY — no table lock. Idempotent.
-- =====================================================================

-- CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
-- Supabase's SQL editor and `supabase db push` execute this file in its
-- own connection which handles this correctly.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_event_type_timestamp
    ON public.audit_logs (event_type, timestamp DESC);

COMMENT ON INDEX public.idx_audit_event_type_timestamp IS
    'PERF-001: composite index for /health/operational and event-type + time-range queries.';

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- DROP INDEX CONCURRENTLY IF EXISTS public.idx_audit_event_type_timestamp;
