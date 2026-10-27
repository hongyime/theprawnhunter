-- Disk IO reduction: per-row triggers on exfiltrated_messages issued a single-row
-- HOT UPDATE on monitor_stats for every insert/update/delete. With ~2.2M row
-- updates that is millions of dead tuples, WAL writes and autovacuum passes on
-- a one-row table. Replace with a periodic recompute (1 write/minute).

DROP TRIGGER IF EXISTS trg_monitor_stats_messages_delta ON public.exfiltrated_messages;
DROP TRIGGER IF EXISTS trg_monitor_stats_credentials_delta ON public.discovered_credentials;
DROP FUNCTION IF EXISTS public.monitor_stats_messages_delta();
DROP FUNCTION IF EXISTS public.monitor_stats_credentials_delta();

CREATE OR REPLACE FUNCTION public.refresh_monitor_stats()
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    INSERT INTO public.monitor_stats AS m (id, credentials_total, credentials_active,
                                           messages_exfiltrated, messages_broadcasted, refreshed_at)
    SELECT TRUE,
           (SELECT count(*) FROM public.discovered_credentials),
           (SELECT count(*) FROM public.discovered_credentials WHERE status = 'active'),
           (SELECT count(*) FROM public.exfiltrated_messages),
           (SELECT count(*) FROM public.exfiltrated_messages WHERE is_broadcasted = TRUE),
           now()
    ON CONFLICT (id) DO UPDATE SET
        credentials_total    = EXCLUDED.credentials_total,
        credentials_active   = EXCLUDED.credentials_active,
        messages_exfiltrated = EXCLUDED.messages_exfiltrated,
        messages_broadcasted = EXCLUDED.messages_broadcasted,
        refreshed_at         = EXCLUDED.refreshed_at;
$$;

REVOKE ALL ON FUNCTION public.refresh_monitor_stats() FROM PUBLIC;
