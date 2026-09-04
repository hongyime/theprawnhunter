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
            'type=%s entity=%s evidence=%s confidence=%.2f => severity=%s priority=%s',
            p_finding_type,
            COALESCE(p_entity_type, 'unknown'),
            v_ev,
            v_conf,
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
