-- Migration: private evidence-thumbnail bucket for the analyst dashboard
-- Purpose:
--   TODO 43 uploads a bounded JPEG thumbnail for every captured image to
--   storage path <credential_id>/<message_id>.jpg. The dashboard (TODO 44)
--   signs those paths with the authenticated Supabase client, so the bucket
--   must stay PRIVATE and only signed-in users may read the objects.
--
-- Access model:
--   - the bucket is private (public => false); there is no anon/PUBLIC policy,
--     so an unauthenticated client cannot read or list thumbnails.
--   - `authenticated` gets SELECT on this bucket's objects, which is what the
--     dashboard's createSignedUrl call needs.
--   - writes are performed by the backend with the service_role key, which
--     bypasses RLS; no extra grant or policy is added for it.
--
-- Idempotent: the bucket insert is ON CONFLICT DO NOTHING (then an explicit
-- `public = false` update repairs an existing public bucket), and the policy is
-- dropped before being recreated.

insert into storage.buckets (id, name, public)
values ('evidence-thumbs', 'evidence-thumbs', false)
on conflict (id) do nothing;

update storage.buckets
   set public = false
 where id = 'evidence-thumbs';

drop policy if exists "evidence_thumbs_authenticated_read" on storage.objects;

create policy "evidence_thumbs_authenticated_read"
  on storage.objects
  for select
  to authenticated
  using (bucket_id = 'evidence-thumbs');
