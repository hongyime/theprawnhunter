-- Explicit, archive-before-delete retention operation.
--
-- SAFE DEFAULT: this block only reports candidate counts. To execute, an
-- operator must deliberately change both values below, review the counts, take
-- a database backup, and run the block again:
--
--     v_dry_run := FALSE;
--     v_confirmation := 'PURGE_ARCHIVED_ROWS';
--
-- This script is intentionally not a Supabase migration and installs no cron
-- job. Run it with a service-role/database-owner session only.
DO $retention_cleanup$
DECLARE
    v_dry_run BOOLEAN := TRUE;
    v_confirmation TEXT := '';
    v_now TIMESTAMPTZ := clock_timestamp();
    v_run_id UUID := gen_random_uuid();
    v_batch_id UUID := gen_random_uuid();
    v_count BIGINT;
    v_results JSONB := '{}'::jsonb;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM public.exfiltrated_messages AS message
    WHERE (message.is_broadcasted IS TRUE AND message.created_at < v_now - INTERVAL '30 days')
       OR (message.is_broadcasted IS NOT TRUE AND message.created_at < v_now - INTERVAL '90 days');
    v_results := v_results || jsonb_build_object('exfiltrated_messages', v_count);

    SELECT COUNT(*) INTO v_count
    FROM public.audit_logs AS audit
    WHERE audit.timestamp < v_now - INTERVAL '14 days';
    v_results := v_results || jsonb_build_object('audit_logs', v_count);

    SELECT COUNT(*) INTO v_count
    FROM public.honeypot_updates AS update_row
    WHERE update_row.received_at < v_now - INTERVAL '30 days'
      AND update_row.redirected_at IS NOT NULL;
    v_results := v_results || jsonb_build_object('honeypot_updates', v_count);

    SELECT COUNT(*) INTO v_count
    FROM public.keepalive_log AS keepalive
    WHERE keepalive.created_at < v_now - INTERVAL '7 days';
    v_results := v_results || jsonb_build_object('keepalive_log', v_count);

    SELECT COUNT(*) INTO v_count
    FROM public.engagement_events AS engagement
    WHERE engagement.expires_at < v_now;
    v_results := v_results || jsonb_build_object('engagement_events', v_count);

    SELECT COUNT(*) INTO v_count
    FROM public.telemetry_indicators AS indicator
    WHERE indicator.first_seen_at < v_now - INTERVAL '180 days'
       OR EXISTS (
            SELECT 1
            FROM public.exfiltrated_messages AS message
            WHERE message.id = indicator.message_id
              AND (
                    (message.is_broadcasted IS TRUE AND message.created_at < v_now - INTERVAL '30 days')
                 OR (message.is_broadcasted IS NOT TRUE AND message.created_at < v_now - INTERVAL '90 days')
              )
       );
    v_results := v_results || jsonb_build_object('telemetry_indicators', v_count);

    SELECT COUNT(*) INTO v_count
    FROM public.finding_evidence AS evidence
    WHERE evidence.last_seen_at < v_now - INTERVAL '180 days'
       OR EXISTS (
            SELECT 1
            FROM public.finding_summaries AS finding
            WHERE finding.finding_id = evidence.finding_id
              AND finding.last_seen_at < v_now - INTERVAL '730 days'
       );
    v_results := v_results || jsonb_build_object('finding_evidence', v_count);

    SELECT COUNT(*) INTO v_count
    FROM public.finding_summaries AS finding
    WHERE finding.last_seen_at < v_now - INTERVAL '730 days';
    v_results := v_results || jsonb_build_object('finding_summaries', v_count);

    RAISE NOTICE 'Retention candidates: %', v_results;

    IF v_dry_run THEN
        RAISE NOTICE 'Dry run only: no archive or source rows were written.';
        RETURN;
    END IF;

    IF v_confirmation IS DISTINCT FROM 'PURGE_ARCHIVED_ROWS' THEN
        RAISE EXCEPTION
            'Cleanup refused: set the exact confirmation token after reviewing a dry run.';
    END IF;

    INSERT INTO public.retention_cleanup_runs (
        run_id, archive_batch_id, results
    ) VALUES (
        v_run_id, v_batch_id, v_results
    );

    -- Archive cascade-dependent rows first.
    INSERT INTO public.retention_archive (
        source_table, source_id, source_recorded_at, payload, archived_at, archive_batch_id
    )
    SELECT 'telemetry_indicators', indicator.id::text, indicator.first_seen_at,
           to_jsonb(indicator), v_now, v_batch_id
    FROM public.telemetry_indicators AS indicator
    WHERE indicator.first_seen_at < v_now - INTERVAL '180 days'
       OR EXISTS (
            SELECT 1
            FROM public.exfiltrated_messages AS message
            WHERE message.id = indicator.message_id
              AND (
                    (message.is_broadcasted IS TRUE AND message.created_at < v_now - INTERVAL '30 days')
                 OR (message.is_broadcasted IS NOT TRUE AND message.created_at < v_now - INTERVAL '90 days')
              )
       )
    ON CONFLICT (source_table, source_id) DO UPDATE SET
        source_recorded_at = EXCLUDED.source_recorded_at,
        payload = EXCLUDED.payload,
        archived_at = EXCLUDED.archived_at,
        archive_batch_id = EXCLUDED.archive_batch_id;

    INSERT INTO public.retention_archive (
        source_table, source_id, source_recorded_at, payload, archived_at, archive_batch_id
    )
    SELECT 'finding_evidence', evidence.id::text, evidence.last_seen_at,
           to_jsonb(evidence), v_now, v_batch_id
    FROM public.finding_evidence AS evidence
    WHERE evidence.last_seen_at < v_now - INTERVAL '180 days'
       OR EXISTS (
            SELECT 1
            FROM public.finding_summaries AS finding
            WHERE finding.finding_id = evidence.finding_id
              AND finding.last_seen_at < v_now - INTERVAL '730 days'
       )
    ON CONFLICT (source_table, source_id) DO UPDATE SET
        source_recorded_at = EXCLUDED.source_recorded_at,
        payload = EXCLUDED.payload,
        archived_at = EXCLUDED.archived_at,
        archive_batch_id = EXCLUDED.archive_batch_id;

    INSERT INTO public.retention_archive (
        source_table, source_id, source_recorded_at, payload, archived_at, archive_batch_id
    )
    SELECT 'exfiltrated_messages', message.id::text, message.created_at,
           to_jsonb(message), v_now, v_batch_id
    FROM public.exfiltrated_messages AS message
    WHERE (message.is_broadcasted IS TRUE AND message.created_at < v_now - INTERVAL '30 days')
       OR (message.is_broadcasted IS NOT TRUE AND message.created_at < v_now - INTERVAL '90 days')
    ON CONFLICT (source_table, source_id) DO UPDATE SET
        source_recorded_at = EXCLUDED.source_recorded_at,
        payload = EXCLUDED.payload,
        archived_at = EXCLUDED.archived_at,
        archive_batch_id = EXCLUDED.archive_batch_id;

    INSERT INTO public.retention_archive (
        source_table, source_id, source_recorded_at, payload, archived_at, archive_batch_id
    )
    SELECT 'audit_logs', audit.id::text, audit.timestamp,
           to_jsonb(audit), v_now, v_batch_id
    FROM public.audit_logs AS audit
    WHERE audit.timestamp < v_now - INTERVAL '14 days'
    ON CONFLICT (source_table, source_id) DO UPDATE SET
        source_recorded_at = EXCLUDED.source_recorded_at,
        payload = EXCLUDED.payload,
        archived_at = EXCLUDED.archived_at,
        archive_batch_id = EXCLUDED.archive_batch_id;

    INSERT INTO public.retention_archive (
        source_table, source_id, source_recorded_at, payload, archived_at, archive_batch_id
    )
    SELECT 'honeypot_updates', update_row.id::text, update_row.received_at,
           to_jsonb(update_row), v_now, v_batch_id
    FROM public.honeypot_updates AS update_row
    WHERE update_row.received_at < v_now - INTERVAL '30 days'
      AND update_row.redirected_at IS NOT NULL
    ON CONFLICT (source_table, source_id) DO UPDATE SET
        source_recorded_at = EXCLUDED.source_recorded_at,
        payload = EXCLUDED.payload,
        archived_at = EXCLUDED.archived_at,
        archive_batch_id = EXCLUDED.archive_batch_id;

    INSERT INTO public.retention_archive (
        source_table, source_id, source_recorded_at, payload, archived_at, archive_batch_id
    )
    SELECT 'keepalive_log', keepalive.id::text, keepalive.created_at,
           to_jsonb(keepalive), v_now, v_batch_id
    FROM public.keepalive_log AS keepalive
    WHERE keepalive.created_at < v_now - INTERVAL '7 days'
    ON CONFLICT (source_table, source_id) DO UPDATE SET
        source_recorded_at = EXCLUDED.source_recorded_at,
        payload = EXCLUDED.payload,
        archived_at = EXCLUDED.archived_at,
        archive_batch_id = EXCLUDED.archive_batch_id;

    INSERT INTO public.retention_archive (
        source_table, source_id, source_recorded_at, payload, archived_at, archive_batch_id
    )
    SELECT 'engagement_events', engagement.id::text, engagement.occurred_at,
           to_jsonb(engagement), v_now, v_batch_id
    FROM public.engagement_events AS engagement
    WHERE engagement.expires_at < v_now
    ON CONFLICT (source_table, source_id) DO UPDATE SET
        source_recorded_at = EXCLUDED.source_recorded_at,
        payload = EXCLUDED.payload,
        archived_at = EXCLUDED.archived_at,
        archive_batch_id = EXCLUDED.archive_batch_id;

    INSERT INTO public.retention_archive (
        source_table, source_id, source_recorded_at, payload, archived_at, archive_batch_id
    )
    SELECT 'finding_summaries', finding.finding_id::text, finding.last_seen_at,
           to_jsonb(finding), v_now, v_batch_id
    FROM public.finding_summaries AS finding
    WHERE finding.last_seen_at < v_now - INTERVAL '730 days'
    ON CONFLICT (source_table, source_id) DO UPDATE SET
        source_recorded_at = EXCLUDED.source_recorded_at,
        payload = EXCLUDED.payload,
        archived_at = EXCLUDED.archived_at,
        archive_batch_id = EXCLUDED.archive_batch_id;

    -- Delete only rows whose current snapshot is now present in the archive.
    DELETE FROM public.telemetry_indicators AS indicator
    USING public.retention_archive AS archive
    WHERE archive.source_table = 'telemetry_indicators'
      AND archive.source_id = indicator.id::text
      AND archive.payload = to_jsonb(indicator)
      AND (
            indicator.first_seen_at < v_now - INTERVAL '180 days'
         OR EXISTS (
                SELECT 1 FROM public.exfiltrated_messages AS message
                WHERE message.id = indicator.message_id
                  AND (
                        (message.is_broadcasted IS TRUE AND message.created_at < v_now - INTERVAL '30 days')
                     OR (message.is_broadcasted IS NOT TRUE AND message.created_at < v_now - INTERVAL '90 days')
                  )
         )
      );

    DELETE FROM public.finding_evidence AS evidence
    USING public.retention_archive AS archive
    WHERE archive.source_table = 'finding_evidence'
      AND archive.source_id = evidence.id::text
      AND archive.payload = to_jsonb(evidence)
      AND (
            evidence.last_seen_at < v_now - INTERVAL '180 days'
         OR EXISTS (
                SELECT 1 FROM public.finding_summaries AS finding
                WHERE finding.finding_id = evidence.finding_id
                  AND finding.last_seen_at < v_now - INTERVAL '730 days'
         )
      );

    DELETE FROM public.exfiltrated_messages AS message
    USING public.retention_archive AS archive
    WHERE archive.source_table = 'exfiltrated_messages'
      AND archive.source_id = message.id::text
      AND archive.payload = to_jsonb(message)
      AND (
            (message.is_broadcasted IS TRUE AND message.created_at < v_now - INTERVAL '30 days')
         OR (message.is_broadcasted IS NOT TRUE AND message.created_at < v_now - INTERVAL '90 days')
      );

    DELETE FROM public.audit_logs AS audit
    USING public.retention_archive AS archive
    WHERE archive.source_table = 'audit_logs'
      AND archive.source_id = audit.id::text
      AND archive.payload = to_jsonb(audit)
      AND audit.timestamp < v_now - INTERVAL '14 days';

    DELETE FROM public.honeypot_updates AS update_row
    USING public.retention_archive AS archive
    WHERE archive.source_table = 'honeypot_updates'
      AND archive.source_id = update_row.id::text
      AND archive.payload = to_jsonb(update_row)
      AND update_row.received_at < v_now - INTERVAL '30 days'
      AND update_row.redirected_at IS NOT NULL;

    DELETE FROM public.keepalive_log AS keepalive
    USING public.retention_archive AS archive
    WHERE archive.source_table = 'keepalive_log'
      AND archive.source_id = keepalive.id::text
      AND archive.payload = to_jsonb(keepalive)
      AND keepalive.created_at < v_now - INTERVAL '7 days';

    DELETE FROM public.engagement_events AS engagement
    USING public.retention_archive AS archive
    WHERE archive.source_table = 'engagement_events'
      AND archive.source_id = engagement.id::text
      AND archive.payload = to_jsonb(engagement)
      AND engagement.expires_at < v_now;

    DELETE FROM public.finding_summaries AS finding
    USING public.retention_archive AS archive
    WHERE archive.source_table = 'finding_summaries'
      AND archive.source_id = finding.finding_id::text
      AND archive.payload = to_jsonb(finding)
      AND finding.last_seen_at < v_now - INTERVAL '730 days';

    UPDATE public.retention_cleanup_runs
    SET completed_at = clock_timestamp()
    WHERE run_id = v_run_id;

    RAISE NOTICE 'Confirmed cleanup complete; archive batch: %', v_batch_id;
END
$retention_cleanup$;

-- Space reclamation is a separate, explicit post-cleanup operation. Review
-- archive counts and backups first, then run VACUUM from a non-transactional
-- maintenance session if required.
