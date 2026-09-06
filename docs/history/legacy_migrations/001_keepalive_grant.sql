-- Grant anon role INSERT + DELETE on keepalive_log so the GitHub Actions
-- keepalive workflow can write real DB activity without service_role key.
-- Run once in Supabase SQL editor.

GRANT INSERT, DELETE ON public.keepalive_log TO anon;
