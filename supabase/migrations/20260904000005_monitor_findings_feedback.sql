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
