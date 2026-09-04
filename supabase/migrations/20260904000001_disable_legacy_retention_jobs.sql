-- Disable destructive retention jobs that may have been installed by an
-- earlier version of 20260903000001_supabase_optimization.sql.
--
-- New installations never create these jobs. This forward migration protects
-- already-upgraded environments and is safe when pg_cron is absent or when the
-- jobs have already been removed.
DO $disable_legacy_retention_jobs$
DECLARE
    legacy_job RECORD;
BEGIN
    IF to_regclass('cron.job') IS NULL THEN
        RETURN;
    END IF;

    FOR legacy_job IN
        SELECT jobid
        FROM cron.job
        WHERE jobname = ANY (ARRAY[
            'cleanup-keepalive',
            'cleanup-broadcasted-messages',
            'cleanup-stale-messages',
            'cleanup-audit-logs',
            'cleanup-honeypot-updates',
            'cleanup-telemetry-indicators',
            'cleanup-finding-summaries',
            'cleanup-finding-evidence'
        ])
    LOOP
        PERFORM cron.unschedule(legacy_job.jobid);
    END LOOP;
END
$disable_legacy_retention_jobs$;
