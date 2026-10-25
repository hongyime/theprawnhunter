-- Migration: operator-gated aggregate views for the dashboard
-- Purpose:
--   The dashboard trend lane needs daily capture, takeover and message-volume
--   series plus a bot-health distribution without scanning the raw tables from
--   the browser. These four read-only views aggregate server-side and reuse
--   public.is_dashboard_operator() as the single authorization predicate, so
--   the raw-table grants stay revoked and a signed-in non-operator sees zero
--   rows.
--
--   Contract (todo 39 consumes these exact column names):
--     metrics_capture_daily        (day, source, credentials, messages)
--     metrics_takeover_daily       (day, attempted, succeeded, rate)
--     metrics_bot_health           (status, count, last_validated_day)
--     metrics_message_volume_daily (day, media_type, count)
--
-- Grains:
--   capture        one row per credential discovery day + source. credentials
--                  counts rows created that day; messages counts messages whose
--                  own created_at falls on that same day, attributed to the
--                  capture source of their credential. A FULL OUTER JOIN keeps
--                  days/sources that have messages but no new credentials (and
--                  vice versa), so the series has no holes.
--   takeover       one row per credential discovery day. attempted counts
--                  credentials whose meta carries a webhook_url key (the
--                  validation path records it in meta); succeeded counts those
--                  whose meta reports honeypot_active = true.
--   bot_health     one row per credential status.
--   message_volume one row per message day + media_type.
--
-- Timestamps:
--   last_verified_at is written by the validation worker when an existing
--   credential is re-validated. Newly inserted credentials have no such key,
--   and their insert is itself the validation event, so created_at is the
--   fallback. The regex guard keeps a malformed meta value from aborting the
--   view.
--
-- Indexes:
--   Only the two takeover predicates justify a partial index. Both are
--   keyed on created_at so the daily GROUP BY can read just the matching
--   slices. The existing idx_credentials_webhook_url covers
--   (meta->>'webhook_url') IS NOT NULL, which is not the same predicate as
--   key existence (a JSON null value still has the key), so the new partial
--   index is not redundant. Date-only aggregation views (capture, message
--   volume) have no selective predicate and intentionally get no index: a
--   sequential scan of the narrow date column is cheaper than a full index.
--
-- Access:
--   Same shape as 20261025000002_evidence_full_content.sql: definer views
--   (security_invoker = false) with security_barrier = true, PUBLIC and anon
--   denied, authenticated granted SELECT. Role statements are guarded so the
--   migration also applies on a vanilla Postgres without Supabase's roles.
--
-- Idempotent: CREATE OR REPLACE keeps the fixed column contract, indexes use
-- IF NOT EXISTS, and the grants are re-applied on every run.

-- ---------------------------------------------------------------------------
-- Daily capture volume by discovery source
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.metrics_capture_daily
WITH (security_invoker = false, security_barrier = true) AS
WITH credential_daily AS (
    SELECT
        created_at::date AS day,
        COALESCE(source, 'unknown') AS source,
        count(*) AS credentials
    FROM public.discovered_credentials
    GROUP BY created_at::date, COALESCE(source, 'unknown')
),
message_daily AS (
    SELECT
        m.created_at::date AS day,
        COALESCE(c.source, 'unknown') AS source,
        count(*) AS messages
    FROM public.exfiltrated_messages AS m
    JOIN public.discovered_credentials AS c ON c.id = m.credential_id
    GROUP BY m.created_at::date, COALESCE(c.source, 'unknown')
)
SELECT
    COALESCE(c.day, m.day) AS day,
    COALESCE(c.source, m.source) AS source,
    COALESCE(c.credentials, 0) AS credentials,
    COALESCE(m.messages, 0) AS messages
FROM credential_daily AS c
FULL OUTER JOIN message_daily AS m
    ON m.day = c.day
   AND m.source = c.source
WHERE public.is_dashboard_operator();

COMMENT ON VIEW public.metrics_capture_daily IS
  'Operator-only daily capture series: one row per credential created_at day + discovery source. messages counts exfiltrated_messages created that day, attributed to the credential source. Rows are filtered by public.is_dashboard_operator(); definer view is a security barrier.';

-- ---------------------------------------------------------------------------
-- Daily webhook takeover outcomes
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.metrics_takeover_daily
WITH (security_invoker = false, security_barrier = true) AS
WITH daily AS (
    SELECT
        created_at::date AS day,
        count(*) FILTER (WHERE meta ? 'webhook_url') AS attempted,
        count(*) FILTER (WHERE meta->>'honeypot_active' = 'true') AS succeeded
    FROM public.discovered_credentials
    GROUP BY created_at::date
)
SELECT
    day,
    attempted,
    succeeded,
    CASE
        WHEN attempted = 0 THEN NULL
        ELSE round(succeeded::numeric / attempted, 4)
    END AS rate
FROM daily
WHERE public.is_dashboard_operator();

COMMENT ON VIEW public.metrics_takeover_daily IS
  'Operator-only daily webhook takeover funnel: attempted = credentials whose meta has a webhook_url key on their discovery day; succeeded = credentials whose meta reports honeypot_active = true. rate = succeeded/attempted (NULL when attempted = 0). Rows are filtered by public.is_dashboard_operator().';

-- ---------------------------------------------------------------------------
-- Bot health distribution
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.metrics_bot_health
WITH (security_invoker = false, security_barrier = true) AS
WITH health AS (
    SELECT
        COALESCE(status, 'unknown') AS status,
        count(*) AS count,
        max(COALESCE(
            CASE
                WHEN meta->>'last_verified_at' ~ '^\d{4}-\d{2}-\d{2}'
                THEN (meta->>'last_verified_at')::timestamptz::date
            END,
            created_at::date
        )) AS last_validated_day
    FROM public.discovered_credentials
    GROUP BY COALESCE(status, 'unknown')
)
SELECT
    status,
    count,
    last_validated_day
FROM health
WHERE public.is_dashboard_operator()
ORDER BY status;

COMMENT ON VIEW public.metrics_bot_health IS
  'Operator-only bot health distribution: one row per credential status with the most recent validation day (meta.last_verified_at, created_at fallback). Rows are filtered by public.is_dashboard_operator().';

-- ---------------------------------------------------------------------------
-- Daily message volume by media type
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.metrics_message_volume_daily
WITH (security_invoker = false, security_barrier = true) AS
SELECT
    created_at::date AS day,
    COALESCE(media_type, 'unknown') AS media_type,
    count(*) AS count
FROM public.exfiltrated_messages
WHERE public.is_dashboard_operator()
GROUP BY created_at::date, COALESCE(media_type, 'unknown');

COMMENT ON VIEW public.metrics_message_volume_daily IS
  'Operator-only daily message volume: one row per exfiltrated_messages created_at day + media_type. Rows are filtered by public.is_dashboard_operator(); definer view is a security barrier.';

-- ---------------------------------------------------------------------------
-- Indexes for the takeover predicates (the only selective predicates here)
-- ---------------------------------------------------------------------------
-- attempted: key existence, not value non-null. Supports metrics_takeover_daily.
CREATE INDEX IF NOT EXISTS idx_discovered_credentials_webhook_key
    ON public.discovered_credentials (created_at)
    WHERE meta ? 'webhook_url';

COMMENT ON INDEX public.idx_discovered_credentials_webhook_key IS
  'Partial index for metrics_takeover_daily attempted branch: credentials whose meta has a webhook_url key, ordered by discovery time. Distinct from idx_credentials_webhook_url (non-null value predicate).';

-- succeeded: the honeypot takeover flag recorded in credential meta.
CREATE INDEX IF NOT EXISTS idx_discovered_credentials_honeypot_active
    ON public.discovered_credentials (created_at)
    WHERE meta->>'honeypot_active' = 'true';

COMMENT ON INDEX public.idx_discovered_credentials_honeypot_active IS
  'Partial index for metrics_takeover_daily succeeded branch: credentials with meta.honeypot_active = true, ordered by discovery time.';

-- ---------------------------------------------------------------------------
-- ACLs: PUBLIC and anon denied, authenticated may SELECT (subject to the
-- operator predicate). Mirrors the evidence view's revoke/grant pattern.
-- ---------------------------------------------------------------------------
REVOKE ALL ON public.metrics_capture_daily FROM PUBLIC;
REVOKE ALL ON public.metrics_takeover_daily FROM PUBLIC;
REVOKE ALL ON public.metrics_bot_health FROM PUBLIC;
REVOKE ALL ON public.metrics_message_volume_daily FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON public.metrics_capture_daily FROM anon';
        EXECUTE 'REVOKE ALL ON public.metrics_takeover_daily FROM anon';
        EXECUTE 'REVOKE ALL ON public.metrics_bot_health FROM anon';
        EXECUTE 'REVOKE ALL ON public.metrics_message_volume_daily FROM anon';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON public.metrics_capture_daily FROM authenticated';
        EXECUTE 'REVOKE ALL ON public.metrics_takeover_daily FROM authenticated';
        EXECUTE 'REVOKE ALL ON public.metrics_bot_health FROM authenticated';
        EXECUTE 'REVOKE ALL ON public.metrics_message_volume_daily FROM authenticated';

        EXECUTE 'GRANT SELECT ON public.metrics_capture_daily TO authenticated';
        EXECUTE 'GRANT SELECT ON public.metrics_takeover_daily TO authenticated';
        EXECUTE 'GRANT SELECT ON public.metrics_bot_health TO authenticated';
        EXECUTE 'GRANT SELECT ON public.metrics_message_volume_daily TO authenticated';
    END IF;
END $$;
