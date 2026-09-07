# The Prawn Hunter Frontend

A Next.js dashboard for browsing discovered Telegram bot tokens and exfiltrated messages.

## Key Models

- **`discovered_credentials_public`** (PostgreSQL View, not editable). Fields:
  - `id` (UUID)
  - `created_at` (TIMESTAMPTZ)
  - `source` (TEXT) — OSINT source (shodan/fofa/github/etc.)
  - `status` (TEXT) — one of `pending`/`active`/`revoked`
  - `meta` (JSONB) — viability score, chat evidence, discovery status

- **`exfiltrated_messages`** (Table). Fields:
  - `id` (UUID)
  - `credential_id` (UUID) — FK to discovered_credentials
  - `telegram_msg_id` (INT)
  - `sender_name` (TEXT)
  - `content` (TEXT)
  - `media_type` (TEXT) — default `text`
  - `file_meta` (JSONB) — media attachments metadata
  - `is_broadcasted` (BOOLEAN)
  - `created_at` (TIMESTAMPTZ)

## Supabase Client

The frontend uses the Supabase anon key from environment variables:
```ts
// frontend/lib/supabase.ts
const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_KEY!
)
```

**Security Note**: The anon key only allows:
- SELECT from `discovered_credentials_public` (safe columns only)
- SELECT from `exfiltrated_messages`
- No direct access to `discovered_credentials` table.

## Data Fetching Patterns

- List credentials: `supabase.from('discovered_credentials_public').select('*').order('created_at', { ascending: false }).limit(100)`
- List messages for a credential: `supabase.from('exfiltrated_messages').select('*').eq('credential_id', id).order('created_at', { ascending: false })`
- Realtime subscription (optional): `supabase.channel('messages').on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'exfiltrated_messages' })`

## Getting Started

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## Deploy

- Set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_KEY` in your platform’s environment.
- Build with `npm run build`.
- Start with `npm start`.

## Learn More

- Next.js docs: https://nextjs.org/docs
- Supabase client for Next.js: https://supabase.com/docs/guides/getting-started/nextjs
