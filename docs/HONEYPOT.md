# Honeypot Mode — Push Webhook Receiver

Real-time message interception from compromised bots after webhook takeover.

---

## Overview

When enabled, the system:
1. Detects third-party webhooks registered on captured bot tokens
2. Deletes them via `deleteWebhook`
3. Re-registers the webhook to point at YOUR public endpoint
4. Telegram **pushes** all future messages to your API in real-time
5. Messages are stored in `honeypot_updates` table
6. Optional: auto-reply to users with migration redirect

**Benefit:** No polling. Messages arrive the instant they're sent to the compromised bot.

---

## Architecture

```
┌─────────────────┐
│ Captured Bot    │ (Telegram Bot API)
│ (Third-party   │
│  webhook taken) │
└────────┬────────┘
         │ push messages
         ▼
┌──────────────────────────┐
│ winnethepooh.hong-yi.me │  ← Cloudflare Tunnel (443 → localhost:8011)
│ /honeypot/receive/{id}  │
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│ honeypot_updates table   │  ← Supabase
│ - payload (JSONB)        │
│ - sender_user_id         │
│ - update_type            │
│ - redirected_at          │
└────────┬─────────────────┘
         │
         ├──► flow.honeypot_redirect_sweep (30s)
         │     └──► sendMessage FROM captured bot TO user
         │           └──► "Go to @bryanseahbot?start=migrate"
         │
         └──► ALERT_WEBHOOK_URL (if set)
               └──► Slack/Splunk/MISC notification
```

---

## Configuration

### Required Environment Variables

```bash
# Enable honeypot mode globally
HONEYPOT_MODE=True

# Public HTTPS endpoint (Telegram won't POST to HTTP)
HONEYPOT_WEBHOOK_URL=https://winnethepooh.hong-yi.me/honeypot

# Secret token (32+ random chars) — sent as X-Telegram-Bot-Api-Secret-Token header
HONEYPOT_SECRET=your-random-32-char-string-here

# Which bots to honeypot
HONEYPOT_ALLOWLIST=AUTO  # or comma-separated UUIDs: "uuid1,uuid2,uuid3"
```

### Optional: User Redirect

```bash
# Auto-reply to captured users with migration link
HONEYPOT_REDIRECT_MODE=True
HONEYPOT_REDIRECT_BOT=bryanseahbot           # Your onboard bot username
HONEYPOT_REDIRECT_DEEPLINK=migrate           # ?start=<this> param
```

### Optional: External Alerts

```bash
# POST JSON to Slack/Splunk/MISC on each capture
ALERT_WEBHOOK_URL=https://hooks.slack.com/services/...
ALERT_WEBHOOK_SECRET=optional-auth-header    # Sent as X-Webhook-Secret
```

---

## Deployment

### 1. Cloudflare Tunnel Setup

```powershell
# One-time setup (automated)
.\scripts\setup_cloudflare_tunnel.ps1
```

This creates:
- Named tunnel: `prawnhunter`
- DNS CNAME: `winnethepooh.hong-yi.me` → `<tunnel-id>.cfargotunnel.com`
- Windows service: auto-starts on boot

### 2. WAF Rules

Apply these 5 rules in Cloudflare Dashboard → hong-yi.me → Security → WAF:

| Rule | Purpose |
|------|---------|
| 1 | Block all except honeypot POSTs + monitor-key requests |
| 2 | Rate-limit `/honeypot/receive` (100 req/10s per IP) |
| 3 | Block bot scanners (nmap, sqlmap, nuclei, etc.) |
| 4 | Challenge path probes (`/../`, `/.env`, `/wp-*`) |
| 5 | Geo-restrict to Telegram IP ranges (optional) |

See: [`docs/cloudflare_waf_rules.md`](cloudflare_waf_rules.md)

### 3. Database Migrations

```bash
# Both migrations are idempotent
# Apply via Supabase SQL Editor:

# 1. Honeypot table
database/migrations/20260803000012_honeypot.sql

# 2. Redirect tracking columns
database/migrations/20260806000001_honeypot_redirect.sql
```

---

## API Endpoints

### `POST /honeypot/receive/{credential_id}`

Receive webhook POST from Telegram.

**Auth:** Requires `X-Telegram-Bot-Api-Secret-Token` header matching `HONEYPOT_SECRET`.

**Response:** Always returns `200 OK `{ "ok": true }` — Telegram retries non-2xx aggressively.

**Stored in DB:**
```json
{
  "id": "uuid",
  "credential_id": "uuid",
  "update_type": "message|callback_query|inline_query|...",
  "payload": { /* full Telegram update JSON */ },
  "received_at": "2026-08-29T10:30:00Z",
  "source_ip": "149.154.160.10",
  "sender_user_id": 123456789,
  "redirected_at": null
}
```

### `GET /honeypot/status`

Check honeypot configuration state.

**Auth:** Requires `X-Monitor-Key` header.

**Response:**
```json
{
  "mode_enabled": true,
  "receiver_url_configured": true,
  "secret_configured": true,
  "allowlist_mode": "auto_all_webhook_bots",
  "allowlist_size": "unlimited (auto)"
}
```

---

## Celery Tasks

### `flow.honeypot_redirect_sweep`

Sweeps `honeypot_updates` for un-redirected messages and sends each user a migration reply.

- **Schedule:** Every 30 seconds
- **Dedup:** Permanent Redis key per-user-per-credential
- **Message sent FROM captured bot:**
  ```
  ⚠️ This service has been migrated.
  
  Your request could not be processed here.
  To continue, use the updated channel:
  👉 https://t.me/bryanseahbot?start=migrate
  
  This is an automated notification.
  ```

### `flow.honeypot_redirect_one`

Sends a single redirect message (spawned-by-sweep).

### `flow.force_webhook_takeover_pass`

Queue immediate exfiltrate for all active webhook-registered bots.

- **Schedule:** Every 6 hours
- **Manual trigger:**
  ```bash
  docker exec theprawnhunter_worker-core celery -A app.workers.celery_app call flow.force_webhook_takeover_pass
  ```

---

## Database Schema

### `honeypot_updates`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `credential_id` | UUID | FK to `discovered_credentials` |
| `update_type` | TEXT | `message`, `callback_query`, `inline_query`, etc. |
| `payload` | JSONB | Full Telegram update |
| `received_at` | TIMESTAMPTZ | When captured |
| `source_ip` | TEXT | Telegram's webhook source IP |
| `sender_user_id` | BIGINT | Extracted from `payload.message.from.id` |
| `redirected_at` | TIMESTAMPTZ | When migration reply was sent |
| `redirected_bot` | TEXT | Bot username used for redirect |
| `redirect_error` | TEXT | If send failed |

**Indexes:**
- `idx_honeypot_credential` — query by credential
- `idx_honeypot_received` — time-series queries
- `idx_honeypot_type` — filter by update type
- `idx_honeypot_unredir` — sweep unprocessed messages
- `idx_honeypot_sender` — attribution graph queries

---

## Monitoring

### Check Status

```bash
curl -H "X-Monitor-Key: $MONITOR_API_KEY" https://winnethepooh.hong-yi.me/honeypot/status
```

### Query Recent Captures

```sql
SELECT 
  credential_id,
  update_type,
  payload->'message'->'from'->>'id' as sender_user_id,
  payload->'message'->>'text' as message_text,
  received_at
FROM honeypot_updates
ORDER BY received_at DESC
LIMIT 20;
```

---

## Security Notes

1. **HONEYPOT_SECRET** must be 32+ random chars — used to authenticate Telegram's webhook POSTs
2. **HONEYPOT_ALLOWLIST=AUTO** grows with your DB — only active webhook-registered bots are targeted
3. **Counter-takeover detection** — if third party re-registers over our webhook, we log and re-takeover
4. **WAF Rule 5** (geo-restrict) is aggressive — may break if Telegram adds new IP ranges
5. **No path secrets** — header-based auth (`X-Telegram-Bot-Api-Secret-Token`) is safer than embedded path secrets

---

## Troubleshooting

### "Honeypot mode disabled"

Check `.env`:
```bash
HONEYPOT_MODE=True
HONEYPOT_WEBHOOK_URL=https://winnethepooh.hong-yi.me/honeypot
HONEYPOT_SECRET=<non-empty>
```

### No messages arriving

1. Check tunnel is running: `Get-Service cloudflared`
2. Check WAF rules aren't blocking Telegram IPs
3. Check bot status in DB: `SELECT status FROM discovered_credentials WHERE id = '...'`
4. Check logs: `docker logs theprawnhunter_api --tail 100 | Select-String "Honeypot"`

### Webhook not registered

Run force takeover pass:
```bash
docker exec theprawnhunter_worker-core celery -A app.workers.celery_app call flow.force_webhook_takeover_pass
```

---

## Related Files

- [`app/api/routers/honeypot.py`](../app/api/routers/honeypot.py) — API endpoints
- [`app/services/_scraper/strategies.py`](../app/services/_scraper/strategies.py) — Webhook takeover logic
- [`app/workers/tasks/flow_tasks.py`](../app/workers/tasks/flow_tasks.py) — Redirect sweep tasks
- [`scripts/setup_cloudflare_tunnel.ps1`](../scripts/setup_cloudflare_tunnel.ps1) — Tunnel setup
- [`docs/cloudflare_waf_rules.md`](cloudflare_waf_rules.md) — WAF configuration
- [`supabase/migrations/20260803000012_honeypot.sql`](../supabase/migrations/20260803000012_honeypot.sql) — Table schema
- [`supabase/migrations/20260806000001_honeypot_redirect.sql`](../supabase/migrations/20260806000001_honeypot_redirect.sql) — Redirect tracking
