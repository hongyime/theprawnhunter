-- Add confidence_score to the public view used by the frontend (anon key / RLS-safe)
-- Also retains chat_member_count which was already present in the view.
CREATE OR REPLACE VIEW discovered_credentials_public AS
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
