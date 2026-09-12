# The Prawn Hunter

A self-hosted OSINT pipeline that discovers exposed Telegram Bot API tokens across 21 public data sources, validates each against the live Telegram API, harvests accessible chat history via Telethon and the Bot API, and delivers findings to a private Telegram supergroup organised as per-bot forum topics. Delivered as a Docker Compose stack of 10 services backed by Supabase managed PostgreSQL and Redis. A read-only analyst dashboard is hosted at `theprawnhunter.vercel.app`.

---

## Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Docker Engine | 24.x | Tested on 29.x |
| Docker Compose | v2 (bundled with Docker) | Use `docker compose`, not `docker-compose` |
| Python | 3.11+ | Local dev and tests only — workers run Python 3.11 inside containers |
| Node.js | 18+ | Frontend local dev only |
| Supabase project | Free tier | 500 MB DB limit; Pro recommended for sustained use |
| Telegram account | Any | Required for `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` |
| Telegram bot(s) | One or more | Created via `@BotFather`; must be admin in the monitor supergroup |
| `git` | Any recent | Required for cloning |

---

## Installation

### 1. Clone

```bash
git clone https://github.com/<owner>/theprawnhunter.git
cd theprawnhunter
```

### 2. Create environment file

```bash
cp .env.template .env
# Open .env and fill in every required variable
```

Minimum required before first start:

```
SUPABASE_URL
SUPABASE_KEY
SUPABASE_SERVICE_ROLE_KEY
REDIS_URL=redis://redis:6379/0
ENCRYPTION_KEY          # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
MONITOR_BOT_TOKEN       # from @BotFather — comma-separated for multiple bots
MONITOR_GROUP_ID        # numeric supergroup ID (negative integer)
TELEGRAM_API_ID         # from https://my.telegram.org
TELEGRAM_API_HASH       # from https://my.telegram.org
MONITOR_API_KEY         # any strong random string
FLOWER_BASIC_AUTH       # user:password — must not be admin:changeme
```

### 3. Apply the database schema

In the **Supabase SQL editor** (or via the Management API), run each file in `supabase/migrations/` in filename order. All are idempotent. Using the Management API:

```powershell
# PowerShell — requires your Supabase Personal Access Token
$token = '<your-personal-access-token>'  # Dashboard → Account → Access Tokens
$ref   = '<your-project-ref>'
$url   = "https://api.supabase.com/v1/projects/$ref/database/query"
$h     = @{ Authorization = "Bearer $token"; 'Content-Type' = 'application/json' }

Get-ChildItem supabase/migrations/*.sql | Sort-Object Name | ForEach-Object {
    $body = @{ query = (Get-Content $_.FullName -Raw) } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Post -Uri $url -Headers $h -Body $body
    Write-Host "applied: $($_.Name)"
}
```

### 4. Create external Docker volumes (first-time only)

The stack uses externally-named volumes with a legacy prefix. Create them once before the first `up`:

```bash
docker volume create telegramhunter_redis_data
docker volume create telegramhunter_sessions
docker volume create telegramhunter_imports
docker volume create telegramhunter_beat_schedule
```

### 5. Build and start

```bash
docker compose up -d --build
```

Initial build takes 3–8 minutes. All 10 services start.

### 6. Verify startup

```bash
curl http://localhost:8011/
curl -H "X-Monitor-Key: <your-key>" http://localhost:8011/health/detailed
```

Expected liveness: `{"status":"active"}`. Expected detailed: HTTP 200 with `db`, `redis`, `bot_api` all healthy.

---

## Environment Configuration

Copy `.env.template` → `.env`. Values are never shown here — names only.

### Required

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Supabase project URL (`https://<ref>.supabase.co`) |
| `SUPABASE_KEY` | Supabase anon key — frontend and extension |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role key — bypasses RLS; backend workers only |
| `REDIS_URL` | Redis connection string (`redis://redis:6379/0` for Docker) |
| `ENCRYPTION_KEY` | 44-character Fernet key |
| `MONITOR_BOT_TOKEN` | Comma-separated bot tokens; these bots post to the supergroup |
| `MONITOR_GROUP_ID` | Numeric supergroup ID (negative integer or `@username`) |
| `TELEGRAM_API_ID` | From https://my.telegram.org |
| `TELEGRAM_API_HASH` | 32-character hex from https://my.telegram.org |
| `MONITOR_API_KEY` | Protects all `/monitor/*` and `/health/detailed` endpoints |
| `FLOWER_BASIC_AUTH` | `user:password` for Flower; stack refuses to start on `admin:changeme` |

### Optional — Operations

| Variable | Default | Purpose |
|---|---|---|
| `ENV` | `development` | Set `production` to disable `/docs` and `/scan/trigger` |
| `PLAINTEXT_TOKEN_MODE` | `False` | Bypass Fernet — tokens stored as plaintext. Operator override. |
| `ENCRYPTION_KEY_LEGACY` | (unset) | Comma-separated previous Fernet keys for decryption |
| `PSEUDONYMIZATION_KEY` | (unset) | Stable HMAC key for pseudonymous analyst identifiers |
| `BROADCAST_INTERVAL_MINUTES` | `1` | Broadcast task cadence |
| `BROADCAST_BATCH_SIZE` | `200` | Messages per broadcast run; use `50` on single-bot setups |
| `BROADCAST_MAX_PARALLEL_TOPICS` | `5` | Concurrent topic groups; set equal to bot count |
| `BROADCAST_INTER_MESSAGE_DELAY_SECONDS` | `0.5` | Inter-message delay; use `5.0` on single-bot setups |
| `ENABLE_RAW_MESSAGE_BROADCAST` | `False` | Enable message broadcasting to monitor supergroup |
| `RESCRAPE_INTERVAL_HOURS` | `1` | Re-scrape cadence |
| `SCAN_INTERVAL_HOURS` | `4` | Primary scanner cadence |
| `AUDIT_INTERVAL_HOURS` | `2` | Topic-integrity audit cadence |
| `ALERT_WEBHOOK_URL` | (unset) | POST finding alerts here (Slack / Splunk / MISP) |
| `FINDING_ALERTS_ENABLED` | `False` | Enables outbound alert delivery |
| `HONEYPOT_MODE` | `False` | Enables webhook push receiver |
| `HONEYPOT_WEBHOOK_URL` | (unset) | Public HTTPS URL for honeypot receiver |
| `HONEYPOT_SECRET` | (unset) | Validated against `X-Telegram-Bot-Api-Secret-Token` |
| `HONEYPOT_ALLOWLIST` | `""` | `AUTO` (all taken-over bots) or comma-separated credential UUIDs |
| `HONEYPOT_REDIRECT_MODE` | `True` | Wires up redirect infrastructure |
| `HONEYPOT_REDIRECT_AUTHORIZED` | `False` | Runtime gate — both must be `True` before any redirect sends |
| `HONEYPOT_REDIRECT_BOT` | `<bot-username>` | Target bot for redirect messages |
| `HONEYPOT_REDIRECT_DEEPLINK` | `migrate` | `?start=` parameter on the redirect link |
| `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE` | `False` | Deletes third-party webhooks before polling; destructive |
| `TELEGRAM_HISTORY_TIMEOUT_SECONDS` | `90` | Per-scrape cap on Telethon history reads |
| `AUTO_ARCHIVE_MEDIA` | `False` | Download + re-upload media attachments via Telethon |
| `CANARY_CREDENTIAL_ID` | (unset) | UUID of a synthetic credential for the broadcast canary |
| `TLS_VERIFY_WEBHOOK_PROBES` | `False` | Enforce TLS on webhook C2 host probes |
| `WHITELISTED_BOT_IDS` | `""` | Comma-separated numeric bot IDs to keep in monitor group |
| `PROTECTED_BOT_IDS` | `""` | Comma-separated bot IDs never to scan/validate/broadcast |
| `ALLOW_PUBLIC_STARTHUNTER` | `False` | If `True`, any user can DM the bot to add a session (dangerous) |
| `DATABASE_URL` | (unset) | Postgres DSN for direct `psql`-based migrations |
| `EXTENSION_WRITE_SECRET` | (Supabase DB only) | Set via `ALTER DATABASE postgres SET app.extension_write_secret = '...'` |

### Optional — Scanner API Keys

All degrade gracefully when absent.

| Variable | Scanner |
|---|---|
| `SHODAN_KEY` | Shodan |
| `FOFA_EMAIL` + `FOFA_KEY` | FOFA (paid plan only) |
| `URLSCAN_KEY` | URLScan.io |
| `GITHUB_TOKEN` or `GITHUB_TOKENS` | GitHub Code Search + Gists (comma-separated pool) |
| `GITLAB_TOKEN` | GitLab Blobs |
| `BITBUCKET_USER` + `BITBUCKET_API_TOKEN` | Bitbucket workspace |
| `EXA_API_KEY`, `EXA_API_KEY_2`, `EXA_API_KEY_3` | Exa (keys rotated per request) |
| `PUBLICWWW_KEY` | PublicWWW |
| `GOOGLE_SEARCH_KEY` + `GOOGLE_CSE_ID` | Google Custom Search |
| `NETLAS_API_KEY_1` | Netlas account 1 (50 req/day) |
| `NETLAS_API_KEY_2` | Netlas account 2 (100 req/day) |
| `POSTMAN_API_KEY` | Postman public workspaces |

---

## Running

### Production (Docker Compose)

```bash
docker compose up -d --build   # start all 10 services
docker compose logs -f         # view all logs
docker compose logs -f worker-scrape  # specific service
docker compose down            # stop, preserve volumes
docker compose down -v         # full reset — destroys all data
```

**Services and ports (all bound to 127.0.0.1):**

| Service | Port | URL |
|---|---|---|
| API | 8011 | `http://localhost:8011/` |
| Flower | 8555 | `http://localhost:8555/` |
| Frontend | 3000 | `http://localhost:3000/` |
| Redis | 6379 | `redis://localhost:6379/0` |

### Production overlay

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### Local API development

```bash
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.api.main:app --reload --port 8001
```

### Local frontend development

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000
```

Requires `frontend/.env.local`:
```
NEXT_PUBLIC_SUPABASE_URL=https://<ref>.supabase.co
NEXT_PUBLIC_SUPABASE_KEY=<anon-key>
```

---

## Usage

### Dashboard

Sign in at `https://theprawnhunter.vercel.app/signin` with GitHub. After first login, an admin must set `app_metadata = {"operator": true}` on your Supabase user account to grant access to data.

Dashboard authorization requires a signed-in UUID, the authenticated JWT role and the **JSON boolean** `true` in admin-controlled `app_metadata.operator`. Missing, false, null, string or numeric values do not authorize access; user-editable metadata is never used. Refresh the session after an administrator changes the claim, because an existing JWT keeps its previous claims until refreshed or expired.

Migration `20260912121145_restrict_dashboard_operator_access.sql` restricts the three dashboard tables, checks the existing feedback RPC before privileged writes, and makes the redacted evidence view read-only with an operator filter and security barrier. It preserves the redaction expression and keeps raw-table access revoked. The existing definer view is retained to avoid granting access to private raw fields; the JWT helper uses invoker privileges. No stored records are changed by this migration.

Authorization regression checks run in the `Operator authorization` workflow against synthetic PostgreSQL records without application imports or provider credentials. To run locally, create an empty loopback PostgreSQL database named `prawn_hunter_auth_fixture_<suffix>`, supply standard `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` environment variables, and run `python scripts/test_operator_authorization.py` (`PSQL` optionally selects the client executable). The fixture reproduces the previous bypasses before applying the migration, then exercises real role, RLS, view and RPC allow/deny outcomes. It must never target an application database.

### API — health

```bash
curl http://localhost:8011/health/
curl -H "X-Monitor-Key: <key>" http://localhost:8011/health/detailed
curl -H "X-Monitor-Key: <key>" http://localhost:8011/health/queues
```

### API — credentials

```bash
curl -H "X-Monitor-Key: <key>" \
  "http://localhost:8011/monitor/credentials?sort_by=collection_yield_score&limit=20"
```

### API — findings (analyst queue)

```bash
curl -H "X-Monitor-Key: <key>" \
  "http://localhost:8011/monitor/findings?min_priority=5&limit=50"
```

### API — search

```bash
curl -H "X-Monitor-Key: <key>" \
  "http://localhost:8011/monitor/search?q=bitcoin&limit=50"
```

### Ingest tokens manually

```bash
curl -X POST http://localhost:8011/ingest/tokens \
  -H "X-Monitor-Key: <key>" \
  -H "Content-Type: text/plain" \
  --data-binary @tokens.txt
```

### CSV import

Drop a CSV file into `imports/`:
```csv
token,chat_id
1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx,-1001234567890
```
`system.import_csv` picks it up every 5 minutes. A `.done` breadcrumb is written after successful processing.

### Admin bot commands

| Command | Effect |
|---|---|
| `/status` | Health, pending counts, bot pool info |
| `/pause` | Pause scanners and broadcaster |
| `/resume` | Resume all operations |
| `/bots` | Bot pool status |
| `/starthunter` | Interactive Telethon account login |
| `/restart` | Restart the bot listener |
| `/help` | Full command reference |

---

## Testing

### Install dev dependencies

```bash
pip install -r requirements-dev.txt
```

### Run the full suite

```bash
pytest
```

### Run specific suites

```bash
pytest tests/unit/          # 330 unit tests, no external dependencies
pytest tests/test_api.py    # API endpoint tests
pytest tests/test_security.py
pytest tests/test_auth.py
pytest tests/integration/   # requires live Supabase + Redis
pytest --cov=app --cov-report=html  # with coverage
```

### Frontend tests

```bash
cd frontend
npm test   # tsc --noEmit + vitest
```

### Current state

- **402 tests collected** — 398 pass, 4 skipped, 0 failures (pytest 8.3.5). Stats-cache fixtures are isolated per test; readiness tests cover event-loop responsiveness, bounded shared probes, recovery, and error redaction.
- **Not covered:** `app/services/bot_manager_srv.py`, `app/workers/tasks/pivot_tasks.py`, `app/workers/tasks/firehose_tasks.py`, `app/workers/tasks/import_tasks.py`, `extension/`

---

## Project Structure

```
theprawnhunter/
├── app/
│   ├── api/                 FastAPI app + 6 routers (31 HTTP endpoints)
│   ├── core/                Config, security, audit, Redis, DB, metrics, circuit breakers
│   ├── schemas/             Pydantic request/response models
│   ├── services/            21 scanner classes, scraper, broadcaster, bot listener, user-agent pool
│   │   └── _scraper/        4-strategy scrape lifecycle
│   ├── utils/               HTTP client pool (with retry_with_backoff), token helpers
│   └── workers/
│       ├── celery_app.py    Celery app, persistent event loop, 60-entry beat schedule
│       ├── flower_app.py    Minimal Flower-only Celery app
│       └── tasks/           79 @app.task handlers across 9 modules
├── database/
│   ├── init.sql             Canonical schema DDL
│   ├── rls_policies.sql     Row Level Security policies
│   └── operations/          Retention cleanup SQL
├── docs/
│   ├── PRD.md               This product requirements document
│   ├── SUPABASE_KEEPALIVE.md
│   ├── HONEYPOT.md
│   ├── cloudflare_waf_rules.md
│   ├── production_runbook.md
│   ├── deployment/          Rebuild + migration runbooks
│   ├── plans/               Historical planning documents
│   └── history/             Archived audits, legacy migrations, paste blobs
├── extension/               Manifest V3 Chrome extension (FOFA scraper → ingest)
├── frontend/                Next.js 16 analyst dashboard (TypeScript, Tailwind CSS 4)
├── imports/                 Drop CSV files here for auto-import
├── scripts/                 Operational scripts (apply_migrations.ps1, decrypt_to_plaintext.py, etc.)
├── supabase/
│   ├── config.toml
│   └── migrations/          25 versioned migrations applied to live Supabase
├── tests/
│   ├── unit/                46 unit test modules
│   ├── integration/         3 integration test modules
│   ├── load/                1 load test
│   └── *.py                 9 top-level test modules
├── .env.template            All variable names + purposes
├── docker-compose.yml       10-service production stack
├── docker-compose.prod.yml  Production overrides
├── Dockerfile               Two-stage build (non-root celery user)
├── docker-entrypoint.sh     Container entrypoint
├── pyproject.toml           Ruff + mypy + pytest configuration
└── requirements.txt         Pinned Python runtime dependencies
```

---

## Troubleshooting

### `volume "telegramhunter_redis_data" not found`

Create the four external volumes before first run (one-time):
```bash
docker volume create telegramhunter_redis_data
docker volume create telegramhunter_sessions
docker volume create telegramhunter_imports
docker volume create telegramhunter_beat_schedule
```

### Flower refuses to start

`FLOWER_BASIC_AUTH` must be anything other than `admin:changeme`. Entrypoint exits with an explicit error message if not set.

### Worker shows `unhealthy` after restart

The Redis-ping healthcheck has a 120 s `start_period` and a 180 s timeout. Python import time under CPU pressure can exceed 90 s. Wait for the start period before diagnosing:
```bash
docker inspect theprawnhunter_worker-core --format '{{json .State.Health.Log}}'
```

### Broadcasts show `flood_wait` and not delivering

With a single monitor bot, raise the inter-message delay:
```bash
# .env
BROADCAST_MAX_PARALLEL_TOPICS=1
BROADCAST_INTER_MESSAGE_DELAY_SECONDS=5.0
BROADCAST_BATCH_SIZE=50
```
Then restart worker-core:
```bash
docker compose up -d --no-deps worker-core
```

### "No usable user session" broadcast failures

Media-archive messages require a Telethon session account. Run `/starthunter` in a DM with your monitor bot to add one.

### `/monitor/findings` returns 500

The findings tables are missing. Apply all 25 migrations in `supabase/migrations/` in filename order via the SQL editor or Management API.

### Migrations timing out via Supabase SQL editor

Large `UPDATE` statements on `exfiltrated_messages` hit the 30 s free-tier statement timeout. Use the Management API with 500-row batches:
```powershell
# See scripts/apply_migrations.ps1
```

### Supabase DB over quota

Prune broadcasted messages older than 30 days (all already delivered to Telegram):
```python
from app.core.database import db
from datetime import datetime, timedelta, timezone
cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
# DELETE in 500-row batches via Management API
```
Then run `VACUUM FULL ANALYZE public.exfiltrated_messages;` via Supabase SQL editor to reclaim physical space.

### Dashboard shows "Authenticating..." forever

1. Confirm `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_KEY` are baked into the Vercel build.
2. Confirm `site_url` in Supabase Auth config matches the Vercel deployment URL exactly.
3. Confirm GitHub OAuth callback URL is `https://<ref>.supabase.co/auth/v1/callback`.

### GitHub SSO redirects to 404

The OAuth App Client ID (20-character alphanumeric) and Client Secret (40-character hex) must not be swapped. The Client ID is the shorter value visible on the OAuth App settings page under "Client ID".

---

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
