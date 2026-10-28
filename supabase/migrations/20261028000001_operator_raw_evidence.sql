-- Operator-only raw evidence view. Authenticated operators need sender
-- identity to triage; anon and non-operators must never read it.
CREATE OR REPLACE VIEW public.evidence_operator_raw
WITH (security_invoker = false, security_barrier = true) AS
SELECT
    m.id,
    m.credential_id,
    m.sender_name,
    m.sender_user_id,
    regexp_replace(
        m.content,
        '\d{8,10}:[A-Za-z0-9_-]{30,}',
        '[TOKEN]',
        'g'
    ) AS content,
    m.media_type,
    m.created_at,
    c.chat_id,
    c.chat_name,
    c.source,
    c.status,
    c.bot_username
FROM public.exfiltrated_messages AS m
LEFT JOIN public.discovered_credentials AS c ON c.id = m.credential_id
WHERE public.is_dashboard_operator();

REVOKE ALL ON public.evidence_operator_raw FROM PUBLIC;
REVOKE ALL ON public.evidence_operator_raw FROM anon;
GRANT SELECT ON public.evidence_operator_raw TO authenticated;
