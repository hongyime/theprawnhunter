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
