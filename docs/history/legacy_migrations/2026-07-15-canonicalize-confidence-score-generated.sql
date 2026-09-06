-- Canonicalize confidence_score to the generated-column model used by database/init.sql.
-- This repairs older environments that still have a writable INTEGER column from the
-- earlier migration while preserving any previously computed score in meta JSONB.

DO $$
DECLARE
    is_generated text;
BEGIN
    SELECT a.attgenerated
    INTO is_generated
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname = 'discovered_credentials'
      AND a.attname = 'confidence_score'
      AND a.attnum > 0
      AND NOT a.attisdropped;

    IF is_generated IS NULL THEN
        ALTER TABLE public.discovered_credentials
        ADD COLUMN confidence_score INTEGER GENERATED ALWAYS AS (
            CASE
                WHEN meta ? 'confidence_score'
                  AND jsonb_typeof(meta->'confidence_score') = 'number'
                THEN (meta->>'confidence_score')::int
                ELSE NULL
            END
        ) STORED;
    ELSIF is_generated = '' THEN
        UPDATE public.discovered_credentials
        SET meta = jsonb_set(
            COALESCE(meta, '{}'::jsonb),
            '{confidence_score}',
            to_jsonb(confidence_score),
            true
        )
        WHERE confidence_score IS NOT NULL
          AND (
              NOT (COALESCE(meta, '{}'::jsonb) ? 'confidence_score')
              OR (meta->>'confidence_score') IS DISTINCT FROM confidence_score::text
          );

        DROP INDEX IF EXISTS idx_credentials_confidence;
        DROP INDEX IF EXISTS idx_discovered_credentials_confidence_score;

        ALTER TABLE public.discovered_credentials
        DROP COLUMN confidence_score;

        ALTER TABLE public.discovered_credentials
        ADD COLUMN confidence_score INTEGER GENERATED ALWAYS AS (
            CASE
                WHEN meta ? 'confidence_score'
                  AND jsonb_typeof(meta->'confidence_score') = 'number'
                THEN (meta->>'confidence_score')::int
                ELSE NULL
            END
        ) STORED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_discovered_credentials_confidence_score
    ON public.discovered_credentials (confidence_score DESC NULLS LAST)
    WHERE confidence_score IS NOT NULL;
