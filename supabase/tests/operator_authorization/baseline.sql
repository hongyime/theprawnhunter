-- Synthetic schema reproducing the deployed access contract, not application data.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
END $$;
DO $$ BEGIN
  ASSERT NOT (SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname='authenticated');
  ASSERT NOT (SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname='anon');
END $$;
CREATE SCHEMA auth;
GRANT USAGE ON SCHEMA auth, public TO anon, authenticated, service_role;
CREATE TABLE public.findings(id uuid PRIMARY KEY, status text, assignee text);
CREATE TABLE public.finding_feedback(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), finding_id uuid REFERENCES public.findings(id),
 actor_id uuid, label text, reason_code text, note text, status_after text,
 assignee_after text, suppress_pattern text);
CREATE TABLE public.finding_evidence(id uuid PRIMARY KEY);
CREATE TABLE public.audit_logs(event_type text, user_agent text, success boolean, details jsonb);
CREATE TABLE public.exfiltrated_messages(
 id uuid, credential_id uuid, sender_name text, content text, media_type text,
 is_broadcasted boolean, created_at timestamptz);
INSERT INTO public.findings VALUES('22222222-2222-4222-8222-222222222222','open',NULL);
INSERT INTO public.finding_feedback(finding_id,label) VALUES('22222222-2222-4222-8222-222222222222','baseline');
INSERT INTO public.finding_evidence VALUES('33333333-3333-4333-8333-333333333333');
INSERT INTO public.exfiltrated_messages VALUES('44444444-4444-4444-8444-444444444444',NULL,'Synthetic sender','Synthetic fixture only','text',false,'2026-01-01Z');
CREATE OR REPLACE FUNCTION auth.uid()
 RETURNS uuid
 LANGUAGE sql
 STABLE
AS $function$
  select 
  coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid
$function$;
CREATE OR REPLACE FUNCTION auth.role()
 RETURNS text
 LANGUAGE sql
 STABLE
AS $function$
  select 
  coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  )::text
$function$;
CREATE OR REPLACE FUNCTION auth.jwt()
 RETURNS jsonb
 LANGUAGE sql
 STABLE
AS $function$
  select 
    coalesce(
        nullif(current_setting('request.jwt.claim', true), ''),
        nullif(current_setting('request.jwt.claims', true), '')
    )::jsonb
$function$;
CREATE OR REPLACE FUNCTION public.is_dashboard_operator()
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
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
ALTER TABLE public.findings ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.findings TO authenticated;
GRANT ALL ON public.findings TO service_role;
ALTER TABLE public.finding_feedback ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.finding_feedback TO authenticated;
GRANT ALL ON public.finding_feedback TO service_role;
ALTER TABLE public.finding_evidence ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.finding_evidence TO authenticated;
GRANT ALL ON public.finding_evidence TO service_role;
CREATE POLICY evidence_operator_only ON public.finding_evidence FOR ALL TO authenticated USING (is_dashboard_operator()) WITH CHECK (is_dashboard_operator());
CREATE POLICY finding_evidence_authenticated_read ON public.finding_evidence FOR SELECT TO authenticated USING (true);
CREATE POLICY finding_feedback_operator_only ON public.finding_feedback FOR ALL TO authenticated USING (is_dashboard_operator()) WITH CHECK (is_dashboard_operator());
CREATE POLICY findings_operator_only ON public.findings FOR ALL TO authenticated USING (is_dashboard_operator()) WITH CHECK (is_dashboard_operator());
CREATE VIEW public.evidence_redacted WITH (security_invoker=false) AS  SELECT id,
    credential_id,
    'user_'::text || "substring"(md5(sender_name || 'salt_pr4wn_hunt3r'::text), 1, 8) AS sender_pseudonym,
    regexp_replace("left"(regexp_replace(COALESCE(content, ''::text), '\d{8,10}:[A-Za-z0-9_-]{30,}'::text, '[TOKEN]'::text, 'g'::text), 500), '\d{8,10}:[A-Za-z0-9_-]{30,}'::text, '[TOKEN]'::text, 'g'::text) AS content,
    media_type,
    is_broadcasted,
    created_at
   FROM exfiltrated_messages;
GRANT ALL ON public.evidence_redacted TO authenticated, service_role;
REVOKE ALL ON FUNCTION public.record_finding_feedback(uuid,text,text,text,text,text,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.record_finding_feedback(uuid,text,text,text,text,text,text) TO authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.is_dashboard_operator() TO anon,authenticated,service_role;
