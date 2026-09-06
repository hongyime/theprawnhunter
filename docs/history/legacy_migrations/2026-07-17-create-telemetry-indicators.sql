CREATE TABLE IF NOT EXISTS telemetry_indicators (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    credential_id UUID REFERENCES discovered_credentials(id) ON DELETE CASCADE,
    message_id UUID REFERENCES exfiltrated_messages(id) ON DELETE CASCADE,
    indicator_type VARCHAR(64) NOT NULL,
    indicator_value TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    raw_context JSONB DEFAULT '{}'::jsonb,
    CONSTRAINT unique_indicator_per_message UNIQUE(message_id, indicator_type, indicator_value)
);

CREATE INDEX IF NOT EXISTS idx_telemetry_indicators_type_val ON telemetry_indicators(indicator_type, indicator_value);
CREATE INDEX IF NOT EXISTS idx_telemetry_indicators_cred ON telemetry_indicators(credential_id);
