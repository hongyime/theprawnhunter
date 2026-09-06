-- =====================================================================
-- Migration: 20260906000005_audit_logs_composite_idx.sql
-- Finding:   PERF-001
-- Purpose:   Add a composite (event_type, timestamp DESC) index so
--            /health/operational and other queries that filter on both
--            columns can use a single index scan instead of a bitmap-heap
--            merge across two single-column indexes.
-- Safety:    CONCURRENTLY — no table lock. Idempotent.
-- =====================================================================

-- CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
-- Supabase's SQL editor and `supabase db push` execute this file in its
-- own connection which handles this correctly.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_event_type_timestamp
    ON public.audit_logs (event_type, timestamp DESC);

COMMENT ON INDEX public.idx_audit_event_type_timestamp IS
    'PERF-001: composite index for /health/operational and event-type + time-range queries.';

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- DROP INDEX CONCURRENTLY IF EXISTS public.idx_audit_event_type_timestamp;
