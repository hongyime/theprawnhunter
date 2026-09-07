-- Add dedicated confidence_score column for efficient sorting/filtering.
-- Previously stored only in meta JSONB; this column enables indexed queries.
-- Backfill task (validation.backfill_scoring) populates existing rows.

ALTER TABLE discovered_credentials
ADD COLUMN IF NOT EXISTS confidence_score INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_credentials_confidence
ON discovered_credentials(confidence_score DESC, updated_at DESC);

-- Backfill from meta for rows that already have a score computed
UPDATE discovered_credentials
SET confidence_score = (meta->>'confidence_score')::INTEGER
WHERE meta->>'confidence_score' IS NOT NULL
  AND confidence_score = 0;
