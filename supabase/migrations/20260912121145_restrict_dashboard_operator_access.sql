-- Restrict dashboard access to an authenticated UUID and a JSON boolean true
-- in admin-controlled app_metadata. No application records are changed.
CREATE OR REPLACE FUNCTION public.is_dashboard_operator()
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $function$
  SELECT COALESCE(
    auth.uid() IS NOT NULL
    AND auth.jwt()->>'role' = 'authenticated'
    AND auth.jwt()->'app_metadata'->'operator' = 'true'::jsonb,
    false
  );
$function$;

REVOKE EXECUTE ON FUNCTION public.is_dashboard_operator() FROM PUBLIC, anon;

-- Permissive policies combine with OR: remove the legacy allow-all policy.
DROP POLICY IF EXISTS finding_evidence_authenticated_read ON public.finding_evidence;

-- These restrictive guards also constrain any other permissive client policy.
CREATE POLICY findings_operator_restriction ON public.findings
  AS RESTRICTIVE FOR ALL TO authenticated
  USING (public.is_dashboard_operator())
  WITH CHECK (public.is_dashboard_operator());

CREATE POLICY finding_feedback_operator_restriction ON public.finding_feedback
  AS RESTRICTIVE FOR ALL TO authenticated
  USING (public.is_dashboard_operator())
  WITH CHECK (public.is_dashboard_operator());

CREATE POLICY finding_evidence_operator_restriction ON public.finding_evidence
  AS RESTRICTIVE FOR ALL TO authenticated
  USING (public.is_dashboard_operator())
  WITH CHECK (public.is_dashboard_operator());

-- Keep the existing RPC's write contract, but reject non-operators before
-- any privileged statement. Existing function ACL and signature are preserved.
CREATE OR REPLACE FUNCTION public.record_finding_feedback(p_finding_id uuid, p_label text, p_reason_code text DEFAULT NULL::text, p_note text DEFAULT NULL::text, p_status text DEFAULT NULL::text, p_assignee text DEFAULT NULL::text, p_suppress_pattern text DEFAULT NULL::text)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
DECLARE
    feedback_uuid UUID;
    acting_user UUID := auth.uid();
BEGIN
    IF acting_user IS NULL THEN
        RAISE EXCEPTION 'Authentication required';
    END IF;

    IF NOT public.is_dashboard_operator() THEN
        RAISE EXCEPTION 'Operator authorization required' USING ERRCODE = '42501';
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

-- Preserve redaction and raw-table denial. This existing definer view must
-- filter callers explicitly; granting raw-table SELECT to use an invoker view
-- would expose private fields. The barrier prevents caller predicates from
-- being pushed below this authorization filter. Only SELECT remains available.
CREATE OR REPLACE VIEW public.evidence_redacted
WITH (security_invoker = false, security_barrier = true) AS
 SELECT id,
    credential_id,
    'user_'::text || "substring"(md5(sender_name || 'salt_pr4wn_hunt3r'::text), 1, 8) AS sender_pseudonym,
    regexp_replace("left"(regexp_replace(COALESCE(content, ''::text), '\d{8,10}:[A-Za-z0-9_-]{30,}'::text, '[TOKEN]'::text, 'g'::text), 500), '\d{8,10}:[A-Za-z0-9_-]{30,}'::text, '[TOKEN]'::text, 'g'::text) AS content,
    media_type,
    is_broadcasted,
    created_at
   FROM exfiltrated_messages
  WHERE public.is_dashboard_operator();

REVOKE ALL ON public.evidence_redacted FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.evidence_redacted TO authenticated;

COMMENT ON FUNCTION public.is_dashboard_operator() IS
  'Requires a signed-in UUID, authenticated JWT role and app_metadata.operator JSON boolean true. Claims take effect after token refresh.';
COMMENT ON VIEW public.evidence_redacted IS
  'Read-only redacted evidence for authorized operators; raw-table grants remain revoked. Authorization filter uses a security barrier.';
