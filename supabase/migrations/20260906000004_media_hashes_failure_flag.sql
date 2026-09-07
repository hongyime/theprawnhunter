-- =====================================================================
-- Migration: 20260906000004_media_hashes_failure_flag.sql
-- Findings:  INTR-005 (fragile sentinel string), CONC-003 (dup-on-race)
-- Purpose:   Replace the sha256='__failed__<id>' sentinel scheme with a
--            structured is_failure/failure_reason pair, and enforce
--            UNIQUE(message_id) so concurrent hash_exfil_media runs cannot
--            insert duplicate rows for the same message.
-- Safety:    Additive columns + partial unique index + one UPDATE backfill.
--            Idempotent (IF NOT EXISTS guards on additions).
-- =====================================================================

ALTER TABLE public.media_hashes
    ADD COLUMN IF NOT EXISTS is_failure BOOLEAN DEFAULT FALSE;

ALTER TABLE public.media_hashes
    ADD COLUMN IF NOT EXISTS failure_reason TEXT;

-- Migrate existing sentinel rows: any row whose sha256 starts with
-- '__failed__' represents a failed download attempt. Preserve the sentinel
-- string in sha256 for now (existing dedup logic still keys off it) and mark
-- the new boolean so the code can transition cleanly to the flag.
UPDATE public.media_hashes
   SET is_failure = TRUE,
       failure_reason = COALESCE(failure_reason, error, 'legacy_sentinel')
 WHERE is_failure = FALSE
   AND sha256 LIKE '__failed__%';

-- CONC-003: enforce one row per message_id. Prior sentinel scheme guaranteed
-- uniqueness by baking msg_id[:8] into sha256, but two concurrent hash runs
-- could produce identical (real) sha256 rows for the same message_id.
--
-- Guard: only add the unique index if it isn't already present. Use partial
-- form so historical sentinel duplicates (if any) aren't caught retroactively.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = 'public'
           AND indexname  = 'idx_media_hashes_message_id_unique'
    ) THEN
        CREATE UNIQUE INDEX idx_media_hashes_message_id_unique
            ON public.media_hashes (message_id)
            WHERE is_failure = FALSE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_media_hashes_is_failure
    ON public.media_hashes (is_failure)
    WHERE is_failure = TRUE;

COMMENT ON COLUMN public.media_hashes.is_failure IS
    'True when hash_exfil_media could not download the media. See INTR-005.';

COMMENT ON COLUMN public.media_hashes.failure_reason IS
    'Human-readable failure category (download timeout, file not found, decode error, etc.).';

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- DROP INDEX IF EXISTS public.idx_media_hashes_is_failure;
-- DROP INDEX IF EXISTS public.idx_media_hashes_message_id_unique;
-- ALTER TABLE public.media_hashes DROP COLUMN IF EXISTS failure_reason;
-- ALTER TABLE public.media_hashes DROP COLUMN IF EXISTS is_failure;
