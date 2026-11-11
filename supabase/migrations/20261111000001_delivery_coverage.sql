-- Deliver from the real forum-topic stream, not one row per credential.
-- A retry-deferred head row deliberately holds its entire topic so FIFO cannot
-- be violated by a newer row belonging to a sibling credential.

DROP FUNCTION IF EXISTS public.oldest_pending_per_credential(integer);
DROP FUNCTION IF EXISTS public.pending_broadcast_fifo(integer, integer);

CREATE OR REPLACE FUNCTION public.pending_broadcast_fifo(
    p_limit integer DEFAULT 200,
    p_per_topic integer DEFAULT 20
)
RETURNS TABLE (
    id uuid,
    credential_id uuid,
    destination_topic_id bigint,
    destination_topic_closed boolean,
    telegram_msg_id bigint,
    next_retry_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    WITH pending AS (
        SELECT
            m.id,
            m.credential_id,
            (c.meta->>'topic_id')::bigint AS destination_topic_id,
            m.telegram_msg_id,
            m.next_retry_at,
            m.created_at,
            row_number() OVER (
                PARTITION BY (c.meta->>'topic_id')::bigint
                ORDER BY m.created_at, m.telegram_msg_id, m.id
            ) AS topic_position,
            first_value(m.next_retry_at) OVER (
                PARTITION BY (c.meta->>'topic_id')::bigint
                ORDER BY m.created_at, m.telegram_msg_id, m.id
            ) AS head_retry_at,
            bool_or(COALESCE(c.meta ? 'topic_closed_at', FALSE)) OVER (
                PARTITION BY (c.meta->>'topic_id')::bigint
            ) AS destination_topic_closed,
            min(m.created_at) OVER (
                PARTITION BY (c.meta->>'topic_id')::bigint
            ) AS topic_oldest_at
        FROM public.exfiltrated_messages AS m
        JOIN public.discovered_credentials AS c ON c.id = m.credential_id
        WHERE m.is_broadcasted = FALSE
          AND m.telegram_msg_id IS NOT NULL
          AND COALESCE(c.meta->>'topic_id', '') ~ '^[2-9][0-9]*$'
    ), ready_topics AS (
        SELECT *, dense_rank() OVER (ORDER BY topic_oldest_at, destination_topic_id) AS topic_rank
        FROM pending
        WHERE head_retry_at IS NULL OR head_retry_at <= now()
    )
    SELECT id, credential_id, destination_topic_id, destination_topic_closed, telegram_msg_id, next_retry_at
    FROM ready_topics
    WHERE topic_position <= GREATEST(1, LEAST(p_per_topic, 20))
      AND topic_rank <= GREATEST(1, CEIL(GREATEST(1, p_limit)::numeric / GREATEST(1, LEAST(p_per_topic, 20))))
    ORDER BY topic_oldest_at, destination_topic_id, topic_position
    LIMIT GREATEST(1, LEAST(p_limit, 200));
$$;

CREATE OR REPLACE FUNCTION public.topic_pending_count(p_topic_id bigint)
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT count(*)
    FROM public.exfiltrated_messages AS m
    JOIN public.discovered_credentials AS c ON c.id = m.credential_id
    WHERE m.is_broadcasted = FALSE
      AND c.meta->>'topic_id' = p_topic_id::text;
$$;

CREATE OR REPLACE FUNCTION public.monitor_delivery_metrics()
RETURNS TABLE (
    monitorable_topics bigint,
    polled_24h_topics bigint,
    stale_topics bigint,
    no_chat_credentials bigint,
    active_message_topics bigint,
    pending_messages bigint,
    oldest_pending_age_seconds bigint
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    WITH credentials AS (
        SELECT c.*, (c.meta->>'topic_id')::bigint AS topic_id
        FROM public.discovered_credentials AS c
        WHERE c.status = 'active'
          AND COALESCE(c.meta->>'topic_id', '') ~ '^[2-9][0-9]*$'
    ), monitorable AS (
        SELECT * FROM credentials WHERE COALESCE(meta->>'monitorability', 'monitorable') = 'monitorable'
    ), pending AS (
        SELECT m.*, (c.meta->>'topic_id')::bigint AS topic_id
        FROM public.exfiltrated_messages AS m
        JOIN credentials AS c ON c.id = m.credential_id
        WHERE m.is_broadcasted = FALSE
    )
    SELECT
        (SELECT count(DISTINCT topic_id) FROM monitorable),
        (SELECT count(DISTINCT topic_id) FROM monitorable WHERE COALESCE(NULLIF(meta->>'last_polled_at', '')::timestamptz, NULLIF(meta->>'last_recheck_at', '')::timestamptz) >= now() - interval '24 hours'),
        (SELECT count(DISTINCT topic_id) FROM monitorable WHERE COALESCE(NULLIF(meta->>'last_polled_at', '')::timestamptz, NULLIF(meta->>'last_recheck_at', '')::timestamptz) < now() - interval '24 hours' OR COALESCE(NULLIF(meta->>'last_polled_at', '')::timestamptz, NULLIF(meta->>'last_recheck_at', '')::timestamptz) IS NULL),
        (SELECT count(*) FROM public.discovered_credentials WHERE status = 'active' AND COALESCE(meta->>'monitorability', 'monitorable') = 'unmonitorable'),
        (SELECT count(DISTINCT topic_id) FROM pending),
        (SELECT count(*) FROM pending),
        (SELECT EXTRACT(EPOCH FROM now() - min(created_at))::bigint FROM pending);
$$;

REVOKE ALL ON FUNCTION public.pending_broadcast_fifo(integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.topic_pending_count(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.monitor_delivery_metrics() FROM PUBLIC;
