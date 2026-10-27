-- Re-running the entity graph replayed the same 50k edges every 15 minutes and
-- rewrote 2 entity rows + 1 edge row per edge even when nothing changed (~10M
-- row versions/day). Guard each ON CONFLICT update so an unchanged replay is a
-- pure read. RETURNING is empty when the guard skips, so the id is re-selected.

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
    WHERE public.entities.last_seen_at < EXCLUDED.last_seen_at
       OR public.entities.first_seen_at > EXCLUDED.first_seen_at
       OR public.entities.confidence < EXCLUDED.confidence
       OR public.entities.display_value_redacted IS DISTINCT FROM EXCLUDED.display_value_redacted
    RETURNING id INTO source_uuid;

    IF source_uuid IS NULL THEN
        SELECT id INTO source_uuid FROM public.entities
        WHERE entity_type = p_source->>'entity_type'
          AND canonical_value = p_source->>'canonical_value';
    END IF;

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
    WHERE public.entities.last_seen_at < EXCLUDED.last_seen_at
       OR public.entities.first_seen_at > EXCLUDED.first_seen_at
       OR public.entities.confidence < EXCLUDED.confidence
       OR public.entities.display_value_redacted IS DISTINCT FROM EXCLUDED.display_value_redacted
    RETURNING id INTO target_uuid;

    IF target_uuid IS NULL THEN
        SELECT id INTO target_uuid FROM public.entities
        WHERE entity_type = p_target->>'entity_type'
          AND canonical_value = p_target->>'canonical_value';
    END IF;

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
    WHERE public.entity_edges.last_seen_at < EXCLUDED.last_seen_at
       OR public.entity_edges.confidence < EXCLUDED.confidence
    RETURNING id INTO edge_uuid;

    IF edge_uuid IS NULL THEN
        SELECT id INTO edge_uuid FROM public.entity_edges WHERE edge_key = p_edge_key;
    END IF;

    RETURN edge_uuid;
END
$function$;
