-- The batch used to be a global "lowest telegram_msg_id" window, which one
-- chat with low ids could fill entirely. Combined with strict per-topic
-- ordering that collapsed the batch to a single message. Resolve the oldest
-- pending row per credential instead, then order those by age.

CREATE OR REPLACE FUNCTION public.oldest_pending_per_credential(p_limit integer DEFAULT 200)
RETURNS TABLE (id uuid, credential_id uuid, telegram_msg_id bigint)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    WITH oldest AS (
        SELECT DISTINCT ON (m.credential_id)
               m.id, m.credential_id, m.telegram_msg_id
        FROM public.exfiltrated_messages m
        WHERE m.is_broadcasted = FALSE
          AND m.telegram_msg_id IS NOT NULL
          AND (m.next_retry_at IS NULL OR m.next_retry_at <= now())
        ORDER BY m.credential_id, m.telegram_msg_id
    )
    SELECT id, credential_id, telegram_msg_id
    FROM oldest
    ORDER BY telegram_msg_id
    LIMIT GREATEST(1, p_limit);
$$;

REVOKE ALL ON FUNCTION public.oldest_pending_per_credential(integer) FROM PUBLIC;
