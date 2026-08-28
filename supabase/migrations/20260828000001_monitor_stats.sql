-- Migration: maintained monitor stats counters
-- Purpose: keep /monitor/stats O(1) as exfiltrated_messages grows.

CREATE TABLE IF NOT EXISTS public.monitor_stats (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE,
    credentials_total BIGINT NOT NULL DEFAULT 0,
    credentials_active BIGINT NOT NULL DEFAULT 0,
    messages_exfiltrated BIGINT NOT NULL DEFAULT 0,
    messages_broadcasted BIGINT NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT monitor_stats_singleton CHECK (id = TRUE)
);

INSERT INTO public.monitor_stats (
    id,
    credentials_total,
    credentials_active,
    messages_exfiltrated,
    messages_broadcasted,
    refreshed_at
)
SELECT
    TRUE,
    (SELECT COUNT(*) FROM public.discovered_credentials),
    (SELECT COUNT(*) FROM public.discovered_credentials WHERE status = 'active'),
    (SELECT COUNT(*) FROM public.exfiltrated_messages),
    (SELECT COUNT(*) FROM public.exfiltrated_messages WHERE is_broadcasted = TRUE),
    NOW()
ON CONFLICT (id) DO UPDATE SET
    credentials_total = EXCLUDED.credentials_total,
    credentials_active = EXCLUDED.credentials_active,
    messages_exfiltrated = EXCLUDED.messages_exfiltrated,
    messages_broadcasted = EXCLUDED.messages_broadcasted,
    refreshed_at = EXCLUDED.refreshed_at;

CREATE OR REPLACE FUNCTION public.monitor_stats_credentials_delta()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE public.monitor_stats
        SET
            credentials_total = credentials_total + 1,
            credentials_active = credentials_active + CASE WHEN NEW.status = 'active' THEN 1 ELSE 0 END,
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE public.monitor_stats
        SET
            credentials_total = GREATEST(credentials_total - 1, 0),
            credentials_active = GREATEST(credentials_active - CASE WHEN OLD.status = 'active' THEN 1 ELSE 0 END, 0),
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' AND OLD.status IS DISTINCT FROM NEW.status THEN
        UPDATE public.monitor_stats
        SET
            credentials_active = GREATEST(
                credentials_active
                - CASE WHEN OLD.status = 'active' THEN 1 ELSE 0 END
                + CASE WHEN NEW.status = 'active' THEN 1 ELSE 0 END,
                0
            ),
            refreshed_at = NOW()
        WHERE id = TRUE;
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.monitor_stats_messages_delta()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE public.monitor_stats
        SET
            messages_exfiltrated = messages_exfiltrated + 1,
            messages_broadcasted = messages_broadcasted + CASE WHEN NEW.is_broadcasted = TRUE THEN 1 ELSE 0 END,
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE public.monitor_stats
        SET
            messages_exfiltrated = GREATEST(messages_exfiltrated - 1, 0),
            messages_broadcasted = GREATEST(messages_broadcasted - CASE WHEN OLD.is_broadcasted = TRUE THEN 1 ELSE 0 END, 0),
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' AND OLD.is_broadcasted IS DISTINCT FROM NEW.is_broadcasted THEN
        UPDATE public.monitor_stats
        SET
            messages_broadcasted = GREATEST(
                messages_broadcasted
                - CASE WHEN OLD.is_broadcasted = TRUE THEN 1 ELSE 0 END
                + CASE WHEN NEW.is_broadcasted = TRUE THEN 1 ELSE 0 END,
                0
            ),
            refreshed_at = NOW()
        WHERE id = TRUE;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_monitor_stats_credentials_delta ON public.discovered_credentials;
CREATE TRIGGER trg_monitor_stats_credentials_delta
AFTER INSERT OR UPDATE OF status OR DELETE ON public.discovered_credentials
FOR EACH ROW EXECUTE FUNCTION public.monitor_stats_credentials_delta();

DROP TRIGGER IF EXISTS trg_monitor_stats_messages_delta ON public.exfiltrated_messages;
CREATE TRIGGER trg_monitor_stats_messages_delta
AFTER INSERT OR UPDATE OF is_broadcasted OR DELETE ON public.exfiltrated_messages
FOR EACH ROW EXECUTE FUNCTION public.monitor_stats_messages_delta();

CREATE INDEX IF NOT EXISTS idx_messages_broadcasted_true
    ON public.exfiltrated_messages(is_broadcasted)
    WHERE is_broadcasted = TRUE;

CREATE OR REPLACE FUNCTION public.get_monitor_stats()
RETURNS TABLE (
    credentials_total BIGINT,
    credentials_active BIGINT,
    messages_exfiltrated BIGINT,
    messages_broadcasted BIGINT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT
        ms.credentials_total,
        ms.credentials_active,
        ms.messages_exfiltrated,
        ms.messages_broadcasted
    FROM public.monitor_stats AS ms
    WHERE ms.id = TRUE;
$$;

REVOKE ALL ON FUNCTION public.get_monitor_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_monitor_stats() TO service_role;
