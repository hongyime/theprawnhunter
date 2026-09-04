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
