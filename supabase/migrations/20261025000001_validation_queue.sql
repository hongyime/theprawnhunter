-- Migration: priority staging queue for token validation
-- Purpose:
--   Decouple scanner discovery from live Telegram validation. Scanners stage
--   encrypted candidates here with a priority score; the dispatcher leases the
--   highest-priority rows and hands them to validation workers. Rows are
--   encrypted at rest (token_ciphertext) and identified by a stable hash
--   (token_hash) so duplicate candidates collapse without storing plaintext.
--
-- Access: RLS enabled with no anon/authenticated policies. service_role only.
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS public.validation_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash TEXT NOT NULL UNIQUE,
    token_ciphertext TEXT NOT NULL,
    source TEXT,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'done', 'dropped')),
    lease_id UUID,
    lease_expires_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dispatch order: highest priority first, oldest candidate breaks ties.
CREATE INDEX IF NOT EXISTS idx_validation_queue_dispatch
    ON public.validation_queue(status, priority DESC, created_at);

-- Lease reclamation: find rows whose lease expired.
CREATE INDEX IF NOT EXISTS idx_validation_queue_lease
    ON public.validation_queue(lease_id);

-- Keep updated_at truthful without relying on callers.
CREATE OR REPLACE FUNCTION public.validation_queue_touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS validation_queue_touch_updated_at
    ON public.validation_queue;
CREATE TRIGGER validation_queue_touch_updated_at
BEFORE UPDATE ON public.validation_queue
FOR EACH ROW EXECUTE FUNCTION public.validation_queue_touch_updated_at();

-- Security: RLS with no policies denies anon/authenticated. Role statements are
-- guarded so the migration also applies on a vanilla Postgres without Supabase
-- roles.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class
        WHERE oid = to_regclass('public.validation_queue')
    ) THEN
        EXECUTE 'ALTER TABLE public.validation_queue ENABLE ROW LEVEL SECURITY';
        EXECUTE 'REVOKE ALL ON public.validation_queue FROM PUBLIC';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON public.validation_queue FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON public.validation_queue FROM authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT ALL ON public.validation_queue TO service_role';
    END IF;
END $$;
