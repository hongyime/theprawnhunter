CREATE OR REPLACE FUNCTION patch_credential_meta(target_id UUID, patch_key TEXT, patch_data JSONB)
RETURNS VOID LANGUAGE sql SECURITY DEFINER AS $$
  UPDATE discovered_credentials
  SET meta = jsonb_set(coalesce(meta, '{}'::jsonb), ARRAY[patch_key], patch_data, true)
  WHERE id = target_id;
$$;
