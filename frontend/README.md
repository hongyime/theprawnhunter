# The Prawn Hunter Frontend

The dashboard is an operator-only Next.js application. It reads the
operator-filtered `evidence_redacted` view and invokes the `search_evidence`
RPC for search; it does not read raw credential or message tables and does not
subscribe to raw realtime changes.

Configure `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_KEY`, then run:

```bash
npm install
npm run dev
```

For production operations, also configure server-only `API_BASE_URL` and
`MONITOR_API_KEY`. The Next.js operations proxy verifies the signed-in
operator claim before forwarding requests, so the monitor key is never sent to
the browser.

Typical queries use the established safe interfaces:

```ts
supabase.from("evidence_redacted").select("*").limit(100)
supabase.rpc("search_evidence", { search_query: query })
```
