-- Retention prunes by (is_broadcasted, created_at). Without a matching index the
-- scans exceed the PostgREST 3s statement timeout and pruning silently stalls,
-- leaving the free-tier database over its size budget.

CREATE INDEX IF NOT EXISTS idx_messages_broadcasted_created_at
    ON public.exfiltrated_messages (created_at)
    WHERE is_broadcasted = TRUE;

CREATE INDEX IF NOT EXISTS idx_messages_pending_created_at
    ON public.exfiltrated_messages (created_at)
    WHERE is_broadcasted = FALSE;

CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp
    ON public.audit_logs (timestamp);
