-- Strict per-topic ordering: a message may only be sent once every older
-- message in the same credential topic has been sent. Resolving "the oldest
-- pending id per credential" in one call keeps that check to a single query
-- per broadcast batch.

CREATE INDEX IF NOT EXISTS idx_messages_pending_credential_msgid
    ON public.exfiltrated_messages (credential_id, telegram_msg_id)
    WHERE is_broadcasted = FALSE;

CREATE OR REPLACE FUNCTION public.min_pending_message_ids(p_credential_ids uuid[])
RETURNS TABLE (credential_id uuid, min_msg_id bigint)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT credential_id, min(telegram_msg_id)
    FROM public.exfiltrated_messages
    WHERE is_broadcasted = FALSE
      AND credential_id = ANY (p_credential_ids)
      AND telegram_msg_id IS NOT NULL
    GROUP BY credential_id;
$$;

REVOKE ALL ON FUNCTION public.min_pending_message_ids(uuid[]) FROM PUBLIC;
