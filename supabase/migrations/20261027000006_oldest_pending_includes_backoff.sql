-- A message in retry backoff is still the oldest pending item in its topic, so
-- it must block newer messages instead of being skipped. Return its retry time
-- so the caller can hold the whole topic rather than overtaking it.

DROP FUNCTION IF EXISTS public.oldest_pending_per_credential(integer);

CREATE OR REPLACE FUNCTION public.oldest_pending_per_credential(p_limit integer DEFAULT 200)
RETURNS TABLE (id uuid, credential_id uuid, telegram_msg_id bigint, next_retry_at timestamptz)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    WITH oldest AS (
        SELECT DISTINCT ON (m.credential_id)
               m.id, m.credential_id, m.telegram_msg_id, m.next_retry_at
        FROM public.exfiltrated_messages m
        WHERE m.is_broadcasted = FALSE
          AND m.telegram_msg_id IS NOT NULL
        ORDER BY m.credential_id, m.telegram_msg_id
    )
    SELECT id, credential_id, telegram_msg_id, next_retry_at
    FROM oldest
    ORDER BY telegram_msg_id
    LIMIT GREATEST(1, p_limit);
$$;

REVOKE ALL ON FUNCTION public.oldest_pending_per_credential(integer) FROM PUBLIC;
