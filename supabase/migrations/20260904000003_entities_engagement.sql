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
