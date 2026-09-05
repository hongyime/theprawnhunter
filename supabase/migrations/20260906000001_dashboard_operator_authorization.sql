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
