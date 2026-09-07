# theprawnhunter

A self-hosted OSINT pipeline that discovers exposed Telegram Bot API tokens across 21 public data sources, validates each token against the live Telegram API, harvests accessible chat history via Telethon and the Bot API, and delivers findings to a private Telegram supergroup organised as per-bot forum topics. Delivered as a Docker Compose stack of 10 services backed by Supabase managed PostgreSQL and Redis.

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
| `git` | Any recent | Required for cloning and running the repo |

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
# Open .env and fill in every variable marked as required
```

Minimum required variables before first start:

```
SUPABASE_URL
SUPABASE_KEY
SUPABASE_SERVICE_ROLE_KEY
REDIS_URL=redis://redis:6379/0
ENCRYPTION_KEY          # generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
MONITOR_BOT_TOKEN       # from @BotFather, comma-separated for multiple bots
MONITOR_GROUP_ID        # numeric supergroup ID (negative integer)
TELEGRAM_API_ID         # from https://my.telegram.org
TELEGRAM_API_HASH       # from https://my.telegram.org
MONITOR_API_KEY         # any strong random string; protects all /monitor/* and /health/* endpoints
FLOWER_BASIC_AUTH       # user:password — must not be admin:changeme
```

### 3. Apply the database schema

In the **Supabase SQL editor** for your project, run each file in `supabase/migrations/` in filename order (all are idempotent — safe to re-run). The fastest path is to use the Supabase Management API:

```powershell
# PowerShell — requires your Supabase Personal Access Token
$token = '<your-access-token>'  # Dashboard → Account → Access Tokens
$ref   = '<your-project-ref>'   # e.g. xyzabc123
$url   = "https://api.supabase.com/v1/projects/$ref/database/query"
$h     = @{ Authorization = "Bearer $token"; 'Content-Type' = 'application/json' }

Get-ChildItem supabase/migrations/*.sql | Sort-Object Name | ForEach-Object {
    $sql  = Get-Content $_.FullName -Raw
    $body = @{ query = $sql } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Post -Uri $url -Headers $h -Body $body
    Write-Host "applied: $($_.Name)"
}
```

Or use `supabase db push` if you have `DATABASE_URL` set in `.env`.

### 4. Create external Docker volumes

The stack uses externally-named volumes. Create them once before the first `up`:

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

This builds 9 images and starts all 10 services. Initial build takes 3–8 minutes depending on network.

### 6. Verify startup

```bash
# Liveness
curl http://localhost:8011/

# Health (requires MONITOR_API_KEY)
curl -H "X-Monitor-Key: <your-key>" http://localhost:8011/health/detailed
```

Expected output for liveness: `{"status":"active"}`.  
Expected for health: `{"status":"ok", ...}` with `db`, `redis`, `bot_api` all healthy.

---

## Environment Configuration

Copy `.env.template` → `.env` and fill in all required values. Values are never shown here — names only.

### Required

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Supabase project URL (`https://<ref>.supabase.co`) |
| `SUPABASE_KEY` | Supabase anon key — used by frontend and extension |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role key — bypasses RLS; backend workers only, never expose to clients |
| `REDIS_URL` | Redis connection string (`redis://redis:6379/0` for Docker) |
| `ENCRYPTION_KEY` | 44-character Fernet key — generates from `cryptography.fernet.Fernet.generate_key()` |
| `MONITOR_BOT_TOKEN` | Comma-separated bot tokens (`id1:secret1,id2:secret2`); these bots post to the supergroup |
| `MONITOR_GROUP_ID` | Numeric supergroup ID (negative integer or `@username`) |
| `TELEGRAM_API_ID` | From https://my.telegram.org |
| `TELEGRAM_API_HASH` | 32-character hex from https://my.telegram.org |
| `MONITOR_API_KEY` | Protects all `/monitor/*`, `/health/detailed`, `/health/queues` endpoints |
| `FLOWER_BASIC_AUTH` | `user:password` for the Flower dashboard; stack refuses to start if set to `admin:changeme` |

### Optional — Operations

| Variable | Default | Purpose |
|---|---|---|
| `PROJECT_NAME` | `Telegram Hunter` | FastAPI application title |
| `ENV` | `development` | Set to `production` to disable `/docs` and `/scan/trigger` |
| `DEBUG` | `True` | Log verbosity |
| `PLAINTEXT_TOKEN_MODE` | `False` | When `True`, `bot_token` is stored as plaintext (encryption bypassed). Operator override — ensure you understand the security trade-off before enabling. |
| `ENCRYPTION_KEY_LEGACY` | (unset) | Comma-separated previous Fernet keys for decrypting pre-rotation ciphertext |
| `PSEUDONYMIZATION_KEY` | (unset) | Stable HMAC key for pseudonymous analyst identifiers; do not rotate casually |
| `BROADCAST_INTERVAL_MINUTES` | `1` | Broadcast task cadence |
| `BROADCAST_BATCH_SIZE` | `200` | Messages per broadcast run; lower to `50` on single-bot deployments |
| `BROADCAST_MAX_PARALLEL_TOPICS` | `5` | Concurrent topic groups; set equal to number of bots in `MONITOR_BOT_TOKEN` |
| `BROADCAST_INTER_MESSAGE_DELAY_SECONDS` | `0.5` | Inter-message delay; raise to `5.0` on single-bot deployments to avoid flood_wait |
| `RESCRAPE_INTERVAL_HOURS` | `1` | Re-scrape cadence for active credentials |
| `SCAN_INTERVAL_HOURS` | `4` | Primary scanner cadence |
| `AUDIT_INTERVAL_HOURS` | `2` | Topic-integrity audit cadence |
| `ALERT_WEBHOOK_URL` | (unset) | POST policy-routed finding alerts here (Slack/Splunk/MISP) |
| `ALERT_WEBHOOK_SECRET` | (unset) | Sent as `X-Webhook-Secret` header with webhook POSTs |
| `FINDING_ALERTS_ENABLED` | `False` | Enables outbound alert delivery; safe default is off |
| `HONEYPOT_MODE` | `False` | Enables webhook push receiver; requires public HTTPS endpoint |
| `HONEYPOT_WEBHOOK_URL` | (unset) | Public URL for honeypot receiver (must terminate TLS) |
| `HONEYPOT_SECRET` | (unset) | Validated against `X-Telegram-Bot-Api-Secret-Token` header |
| `HONEYPOT_ALLOWLIST` | `""` | `AUTO` (all taken-over bots) or comma-separated credential UUIDs |
| `HONEYPOT_REDIRECT_MODE` | `True` | Wires up redirect infrastructure |
| `HONEYPOT_REDIRECT_AUTHORIZED` | `False` | Runtime gate — both must be `True` before any redirect is sent |
| `HONEYPOT_REDIRECT_BOT` | `<bot-username>` | Target bot for redirect messages |
| `HONEYPOT_REDIRECT_DEEPLINK` | `migrate` | `?start=` parameter on the redirect link |
| `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE` | `False` | Deletes third-party webhooks before polling; destructive |
| `TELEGRAM_HISTORY_TIMEOUT_SECONDS` | `90` | Per-scrape cap on Telethon history reads |
| `AUTO_ARCHIVE_MEDIA` | `False` | Downloads + re-uploads media attachments via Telethon |
| `CANARY_CREDENTIAL_ID` | (unset) | UUID of a synthetic credential used for the broadcast canary |
| `CANARY_EXPECTED_TEXT` | `telegramhunter-canary` | Prefix for synthetic canary message content |
| `CANARY_MAX_AGE_SECONDS` | `1800` | Age budget for canary run |
| `PUBLIC_FRONTEND_URL` | (unset) | Canary checks this URL is reachable |
| `TLS_VERIFY_WEBHOOK_PROBES` | `False` | Enforce TLS on webhook C2 host probes |
| `WHITELISTED_BOT_IDS` | `""` | Comma-separated numeric bot IDs to keep in monitor group |
| `PROTECTED_BOT_IDS` | `""` | Comma-separated bot IDs never to scan/validate/broadcast |
| `ALLOW_PUBLIC_STARTHUNTER` | `False` | If `True`, any user can DM the bot to add a session (dangerous) |
| `ENABLE_RAW_MESSAGE_BROADCAST` | `False` | Enables message broadcasting to monitor supergroup; opt-in |
| `USER_SESSION_STRING` | (unset) | Telethon session string for user-agent invite flow |
| `DATABASE_URL` | (unset) | Postgres DSN for `psql`-based migrations and `schema_drift_check.py` |
| `EXTENSION_WRITE_SECRET` | (stored in Supabase DB only) | Set via `ALTER DATABASE postgres SET app.extension_write_secret = '...'` |

### Optional — Scanner API Keys

All degrade gracefully when absent — the scanner is silently skipped.

| Variable | Scanner |
|---|---|
| `SHODAN_KEY` | Shodan Internet DB |
| `FOFA_EMAIL` + `FOFA_KEY` | FOFA (paid plan only) |
| `URLSCAN_KEY` | URLScan.io |
| `GITHUB_TOKEN` or `GITHUB_TOKENS` | GitHub Code Search + Gists (comma-separated pool) |
| `GITLAB_TOKEN` | GitLab Blobs Search |
| `BITBUCKET_USER` + `BITBUCKET_API_TOKEN` | Bitbucket workspace search |
| `EXA_API_KEY`, `EXA_API_KEY_2`, `EXA_API_KEY_3` | Exa paste/code search; keys rotated per request |
| `PUBLICWWW_KEY` | PublicWWW |
| `GOOGLE_SEARCH_KEY` + `GOOGLE_CSE_ID` | Google Custom Search |
| `NETLAS_API_KEY_1` | Netlas account 1 (50 req/day) |
| `NETLAS_API_KEY_2` | Netlas account 2 (100 req/day) |
| `POSTMAN_API_KEY` | Postman public workspace search |

---

## Running

### Production (Docker Compose)

```bash
# Start all 10 services
docker compose up -d --build

# View all logs
docker compose logs -f

# View a specific service
docker compose logs -f worker-scrape

# Stop (preserves volumes)
docker compose down

# Full reset (destroys all data — volumes included)
docker compose down -v
```

**Services and ports (host-side, all bound to 127.0.0.1):**

| Service | Port | URL |
|---|---|---|
| API | 8011 | `http://localhost:8011/` |
| Flower | 8555 | `http://localhost:8555/` (requires `FLOWER_BASIC_AUTH`) |
| Frontend | 3000 | `http://localhost:3000/` |
| Redis | 6379 | `redis://localhost:6379/0` |

### Production overlay (overrides ENV and concurrency)

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### Local API development (no Docker)

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Export env
export $(grep -v '^#' .env | xargs)   # Linux/macOS
# On Windows: set each variable manually or use a .env loader

# Run
uvicorn app.api.main:app --reload --port 8001
```

### Local frontend development

```bash
cd frontend
npm install
npm run dev    # http://localhost:3000
```

Requires `frontend/.env.local`:
```
NEXT_PUBLIC_SUPABASE_URL=https://<ref>.supabase.co
NEXT_PUBLIC_SUPABASE_KEY=<anon-key>
```

---

## Usage

### Check system health

```bash
# Basic liveness
curl http://localhost:8011/

# Full health (all subsystems)
curl -H "X-Monitor-Key: <key>" http://localhost:8011/health/detailed

# Queue depths
curl -H "X-Monitor-Key: <key>" http://localhost:8011/health/queues
```

### View discovered credentials

```bash
# Most recently found, sorted by confidence score
curl -H "X-Monitor-Key: <key>" \
  "http://localhost:8011/monitor/credentials?sort_by=collection_yield_score&limit=20"
```

### View findings (analyst queue)

```bash
curl -H "X-Monitor-Key: <key>" \
  "http://localhost:8011/monitor/findings?min_priority=5&limit=50"
```

### Search exfiltrated messages

```bash
curl -H "X-Monitor-Key: <key>" \
  "http://localhost:8011/monitor/search?q=bitcoin&limit=50"
```

### Ingest tokens manually (plain text)

```bash
# Newline-separated tokens
curl -X POST http://localhost:8011/ingest/tokens \
  -H "X-Monitor-Key: <key>" \
  -H "Content-Type: text/plain" \
  --data-binary @tokens.txt
```

### CSV import

Drop a CSV file into the `imports/` directory:

```csv
token,chat_id
1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx,-1001234567890
9876543210:AAyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy,
```

The `system.import_csv` task picks it up every 5 minutes. A `.done` breadcrumb is written after successful processing.

### Trigger a manual scanner run (development only)

```bash
# Returns 403 when ENV=production
curl -X POST http://localhost:8011/scan/trigger \
  -H "X-Monitor-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"source":"shodan","query":"telegram bot"}'
```

Valid sources: `shodan`, `fofa`, `github`, `gitlab`, `urlscan`, `sourcegraph`, `searchcode`.

### Admin bot commands

Send these in the monitor supergroup or via DM (whitelisted admins only):

| Command | Effect |
|---|---|
| `/status` | Health, pending counts, bot pool info |
| `/pause` | Pause scanners and broadcaster |
| `/resume` | Resume all operations |
| `/bots` | Bot pool status |
| `/starthunter` | Interactive Telethon account login |
| `/restart` | Restart the bot listener process |
| `/help` | Full command reference |

---

## Testing

### Install test dependencies

```bash
pip install -r requirements-dev.txt
```

### Run the full suite

```bash
pytest
```

### Run specific suites

```bash
# Unit tests (no external dependencies)
pytest tests/unit/

# API tests
pytest tests/test_api.py

# Security tests
pytest tests/test_security.py

# Integration (requires live Supabase + Redis)
pytest tests/integration/

# Write a real Supabase record (opt-in)
ALLOW_SUPABASE_WRITE=1 pytest tests/test_supabase_rw.py

# With coverage
pytest --cov=app --cov-report=html
```

### Test markers

```
@pytest.mark.unit         Unit tests (no external dependencies)
@pytest.mark.integration  Integration tests (may require live services)
@pytest.mark.live         Explicitly opted-in tests calling live external APIs
@pytest.mark.load         Bounded load and latency tests
@pytest.mark.slow         Long-running tests
```

### Frontend tests

```bash
cd frontend
npm test   # runs tsc --noEmit + vitest
```

### Current state

- **399 tests collected** (as of 2026-09-07)
- **394 pass**, 4 skipped, 1 flaky (session-ordering issue in `test_error_hygiene.py`; passes in isolation)
- **Not covered:** `app/services/bot_manager_srv.py`, `app/workers/tasks/pivot_tasks.py`, `app/workers/tasks/firehose_tasks.py`, `app/workers/tasks/import_tasks.py`, `extension/`

---

## Project Structure

```
theprawnhunter/
├── app/
│   ├── api/                 FastAPI app + 6 routers (32 HTTP endpoints)
│   ├── core/                Cross-cutting adapters (config, security, audit, Redis, DB, metrics)
│   ├── schemas/             Pydantic request/response models
│   ├── services/            21 scanner classes, scraper, broadcaster, bot listener, user-agent pool
│   │   └── _scraper/        4-strategy scrape lifecycle (strategies, results, monitor guard, lifecycle)
│   ├── utils/               HTTP client pool, token helpers
│   └── workers/
│       ├── celery_app.py    Celery app, persistent event loop, 60-entry beat schedule
│       ├── flower_app.py    Minimal Celery app for Flower (reads only REDIS_URL)
│       └── tasks/           79 @app.task handlers across 9 modules
│           └── _scanner/    Scanner query library (base + queries)
├── database/
│   ├── init.sql             Canonical schema DDL (idempotent)
│   ├── rls_policies.sql     Row Level Security policies
│   └── operations/          Retention cleanup SQL
├── docs/
│   ├── PRD.md               This product requirements document
│   ├── SUPABASE_KEEPALIVE.md  Keepalive setup guide
│   ├── HONEYPOT.md          Honeypot architecture and configuration
│   ├── cloudflare_waf_rules.md  WAF rule reference
│   ├── production_runbook.md  Operational runbook
│   ├── deployment/          Deployment runbooks (rebuild.md)
│   ├── plans/               Historical planning documents (read-only)
│   └── history/             Archived audit artifacts, legacy migrations, paste blobs
├── extension/               Manifest V3 Chrome extension (FOFA scraper → ingest)
├── frontend/                Next.js 16 analyst dashboard (TypeScript, Tailwind CSS 4)
├── imports/                 Drop CSV files here for auto-import
├── scripts/                 Operational scripts (rotate credentials, schema drift check, etc.)
├── supabase/
│   ├── config.toml
│   └── migrations/          25 versioned migrations applied to live Supabase
├── tests/
│   ├── unit/                46 unit test modules (no external dependencies)
│   ├── integration/         3 integration test modules
│   ├── load/                1 load test
│   └── *.py                 9 top-level test modules
├── .env.template            All variable names + purposes (never commit values)
├── docker-compose.yml       10-service production stack
├── docker-compose.prod.yml  Production overrides (ENV=production, configurable concurrency)
├── Dockerfile               Two-stage build (builder + final, non-root celery user)
├── docker-entrypoint.sh     Container entrypoint (CSV pre-processing, stale lease cleanup)
├── pyproject.toml           Ruff + mypy + pytest configuration
└── requirements.txt         Pinned Python runtime dependencies
```

---

## Troubleshooting

### `volume "telegramhunter_redis_data" not found` on `docker compose up`

Create the external volumes first (one-time setup):

```bash
docker volume create telegramhunter_redis_data
docker volume create telegramhunter_sessions
docker volume create telegramhunter_imports
docker volume create telegramhunter_beat_schedule
```

### Flower refuses to start

`FLOWER_BASIC_AUTH` must be set to a non-default value (anything other than `admin:changeme`). The entrypoint checks this and exits with an error message if not set.

### Worker shows `unhealthy` in `docker compose ps`

The Redis-ping healthcheck can take 30–180 s on startup due to Python import time under CPU pressure. Wait for the `start_period` (120 s) to pass before diagnosing. If it persists:

```bash
docker inspect theprawnhunter_worker-core --format '{{json .State.Health.Log}}'
```

Manual check:
```bash
docker exec theprawnhunter_worker-core python3 -c "import redis, os; redis.from_url(os.environ['REDIS_URL']).ping()"
```

### Broadcasts show `flood_wait` and not delivering

Telegram enforces per-chat flood control. With a single monitor bot:

```bash
# .env — adjust these
BROADCAST_MAX_PARALLEL_TOPICS=1
BROADCAST_INTER_MESSAGE_DELAY_SECONDS=5.0
BROADCAST_BATCH_SIZE=50
```

Restart `worker-core` after changing `.env`:
```bash
docker compose up -d --no-deps worker-core
```

### `/monitor/findings` returns 500

The `findings` table (and related analyst-workflow tables) may not be applied yet. Apply all migrations in `supabase/migrations/` in filename order.

### Migrations timing out via Supabase SQL editor

Large `UPDATE` statements on `exfiltrated_messages` (351 k rows) will hit the 30 s free-tier statement timeout. Use the Management API with 500-row batches instead — see the script in `scripts/apply_migrations.ps1` (requires a Supabase Personal Access Token).

### Supabase DB over quota (500 MB free tier)

Run targeted cleanup:

```python
# Inside worker-core: prune audit_logs older than 7 days (100-row batches)
from app.core.database import db
from datetime import datetime, timedelta, timezone
cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
# loop: db.table('audit_logs').delete().in_('id', ids_batch).execute()
```

Then run `VACUUM FULL ANALYZE public.audit_logs;` via Supabase SQL editor to reclaim physical space. `VACUUM ANALYZE` (without FULL) does not shrink the file.

### Import CSV not picked up

Check that the file has a header row with `token` and `chat_id` columns:
```csv
token,chat_id
1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx,-1001234567890
```

Check the `system.import_csv` task is running in Flower (`http://localhost:8555`).

### Canary `flow.canary_flow_check` always returns `disabled`

Set `CANARY_CREDENTIAL_ID` to the UUID of an existing `discovered_credentials` row and set `ENABLE_RAW_MESSAGE_BROADCAST=True`. The canary also returns disabled if the broadcast flag is off (use `flow.canary_findings_check` instead, which doesn't require raw broadcast).

---

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
