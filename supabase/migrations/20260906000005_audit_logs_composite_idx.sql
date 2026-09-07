-- Migration: 20260906000005_audit_logs_composite_idx.sql
-- Finding:   PERF-001
-- Purpose:   Add a composite (event_type, timestamp DESC) index so
--            /health/operational and other queries that filter on both
--            columns can use a single index scan instead of a bitmap-heap
--            merge across two single-column indexes.
-- Safety:    Non-concurrent build. audit_logs is a write-only table with no
--            reads on the hot path; a brief exclusive lock is acceptable and
--            the Management API executes DDL in a transaction which disallows
--            CONCURRENTLY.

CREATE INDEX IF NOT EXISTS idx_audit_event_type_timestamp
    ON public.audit_logs (event_type, timestamp DESC);

COMMENT ON INDEX public.idx_audit_event_type_timestamp IS
    'PERF-001: composite index for /health/operational and event-type + time-range queries.';

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- DROP INDEX CONCURRENTLY IF EXISTS public.idx_audit_event_type_timestamp;
