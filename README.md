# OSINT Credential Discovery Pipeline

A self-hosted, continuously-running OSINT pipeline that discovers exposed bot tokens across 13 public data sources, validates them against the live API, harvests accessible chat history, and broadcasts findings to a private Telegram supergroup. Delivered as a Docker Compose stack.

- **Runtime:** Python 3.11, FastAPI, Celery, Redis, Telethon
- **Database:** Supabase (managed PostgreSQL) with Row Level Security
- **Frontend (optional):** Next.js 16 + React 19  read-only dashboard
- **Browser Extension (optional):** Manifest V3 Chrome extension  FOFA scraper
- **Deployment:** Docker Compose (10 services)

---

## Prerequisites

| Tool | Minimum Version | Notes |
|---|---|---|
| Docker Engine | 24.x | Tested on 29.x |
| Docker Compose | v2 (bundled) | Use `docker compose`, not `docker-compose` |
| Python | 3.11+ | Local dev and tests only |
| Node.js | 18+ | Frontend only |
| Supabase project |  | Free tier sufficient |
| Telegram account |  | Required for `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` |

---

## Environment Configuration

Copy `.env.template` to `.env` and fill in every value before starting the stack.

```bash
cp .env.template .env
```

### Required Variables

| Variable | Type | Description |
|---|---|---|
| `SUPABASE_URL` | URL | Supabase project URL (`https://<ref>.supabase.co`) |
| `SUPABASE_KEY` | string | Supabase anon key  used by the frontend and extension |
| `SUPABASE_SERVICE_ROLE_KEY` | string | Supabase service-role key  backend only, never expose to clients |
| `REDIS_URL` | URL | Redis connection string (`redis://redis:6379/0` for Docker) |
| `ENCRYPTION_KEY` | 44 chars | Fernet key  generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `MONITOR_BOT_TOKEN` | string | Comma-separated bot tokens used to post findings (e.g. `123:AAA,456:BBB`) |
| `MONITOR_GROUP_ID` | integer | Supergroup chat ID where findings are posted; bot(s) must be admin |
| `TELEGRAM_API_ID` | integer | From https://my.telegram.org |
| `TELEGRAM_API_HASH` | string | 32-character hex from https://my.telegram.org |

### Optional  Operations

| Variable | Default | Description |
|---|---|---|
| `PROJECT_NAME` | `Telegram Hunter` | FastAPI application title |
| `ENV` | `development` | Set to `production` to disable `/docs` and `/scan/trigger` |
| `DEBUG` | `True` | Log verbosity |
| `MONITOR_API_KEY` | *(unset)* | If set, all `/monitor/*` and `/health/detailed` endpoints require `X-Monitor-Key` header |
| `ALERT_WEBHOOK_URL` | *(unset)* | When set, a JSON payload is POSTed here on `credential_activated` and `honeypot_update` events. Supports Slack, Splunk HEC, MISP, or any HTTP endpoint. |
| `ALERT_WEBHOOK_SECRET` | *(unset)* | Sent as `X-Webhook-Secret` header with every webhook POST. Optional auth for your receiver. |
| `WHITELISTED_BOT_IDS` | `""` | Comma-separated bot usernames or IDs kept in the monitor group |
| `ANONYMOUS_ADMIN_ID` | `1087968824` | Telegram anonymous group admin bot ID |
| `USER_SESSION_STRING` | *(unset)* | Telethon session string for user-agent invite flow |
| `BROADCAST_INTERVAL_MINUTES` | `60` | How often pending messages are broadcast |
| `RESCRAPE_INTERVAL_HOURS` | `1` | How often active chats are re-scraped |
| `SCAN_INTERVAL_HOURS` | `4` | Primary scanner cadence |
| `AUDIT_INTERVAL_HOURS` | `2` | Topic-integrity audit cadence |
| `API_PORT` | `8011` | Host-side port for the API service |
| `REDIS_PORT` | `6379` | Host-side port for Redis |
| `FLOWER_PORT` | `8555` | Host-side port for the Flower Celery monitor |
| `COMPOSE_PROJECT_NAME` | `TheprawnHunter` | Docker Compose namespace |
| `EXTENSION_WRITE_SECRET` | *(unset)* | Secret for Chrome extension RLS policy. Must also be set in Supabase: `ALTER DATABASE postgres SET app.extension_write_secret = '<value>';` |
| `TARGET_COUNTRIES` | *(built-in 50-country list)* | Optional JSON array of ISO-3166 codes for country-rotation scanning |
| `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE` | `False` | If `True`, `deleteWebhook` is called on 409 conflicts before polling. Destructive to any third party operating the bot. |
| `TELEGRAM_HISTORY_TIMEOUT_SECONDS` | `90` | Per-scrape cap on Telethon history reads |
| `TELEGRAM_CLIENT_DISCONNECT_TIMEOUT_SECONDS` | `10` | Grace period for lifecycle-safe Telethon cleanup |
| `CANARY_CREDENTIAL_ID` | *(unset)* | UUID of a `discovered_credentials` row used as the synthetic parent for `flow.canary_flow_check`. Canary stays `disabled` until this is set. |
| `CANARY_EXPECTED_TEXT` | `TheprawnHunter-canary` | Prefix for synthetic canary message content |
| `CANARY_MAX_AGE_SECONDS` | `1800` | Age budget for a canary run before it's considered stale |
| `PUBLIC_FRONTEND_URL` | *(unset)* | Optional public URL of the dashboard — canary hits it to verify frontend reachability |

### Optional  Scanner API Keys

All scanner keys degrade gracefully when absent  the corresponding scanner is silently skipped.

| Variable | Scanner |
|---|---|
| `SHODAN_KEY` | Shodan |
| `FOFA_EMAIL` + `FOFA_KEY` | FOFA API (paid plan only) |
| `URLSCAN_KEY` | URLScan.io |
| `EXA_API_KEY`, `EXA_API_KEY_2`, `EXA_API_KEY_3` | Exa paste/code search; extra keys are rotated per request |
| `GITHUB_TOKEN` or `GITHUB_TOKENS` | GitHub Code Search + Gists |
| `GITLAB_TOKEN` | GitLab |
| `BITBUCKET_USER` + `BITBUCKET_API_TOKEN` | Bitbucket (Bearer auth) |
| `PUBLICWWW_KEY` | PublicWWW |
| `SERPER_API_KEY` | Serper (Google SERPs) |
| `GOOGLE_SEARCH_KEY` + `GOOGLE_CSE_ID` | Google Custom Search |
| `NETLAS_API_KEY_1` | Netlas account 1 (50 req/day) |
| `NETLAS_API_KEY_2` | Netlas account 2 (100 req/day) |

If you are not paying for FOFA API access, leave `FOFA_EMAIL` and `FOFA_KEY` empty. The FOFA web / extension collection path still works without API credentials.

---

## Database Setup

Apply the schema to your Supabase project before starting the stack.

1. Open the Supabase SQL editor for your project.
2. Run `database/init.sql`  creates all tables, indexes, views, and the `audit_logs` table.
3. Run `database/rls_policies.sql`  applies Row Level Security policies.
4. If using the Chrome extension with direct Supabase writes, set the write secret:
   ```sql
   ALTER DATABASE postgres SET app.extension_write_secret = 'your-secret-value';
   SELECT pg_reload_conf();
   ```

Both SQL files are idempotent and safe to re-run.

---

## Installation & Setup

### 1. Clone the repository

```bash
git clone <repository-url>
cd <repository-directory>
```

### 2. Configure environment

```bash
cp .env.template .env
# Edit .env  fill in all required variables
```

### 3. Generate an encryption key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Paste the output as ENCRYPTION_KEY in .env
```

### 4. Apply database schema

See [Database Setup](#database-setup) above.

### 5. Start the stack

```bash
docker compose up -d --build
```

This starts 10 services: `redis`, `api`, `worker-core`, `worker-scanners`, `worker-scrape`, `worker-validators`, `beat`, `bot`, `flower`, `frontend`.

### 6. Verify startup

```bash
curl http://localhost:8011/
curl http://localhost:8011/health/
```

Both should return HTTP 200. You can also view the Celery queue monitor at `http://localhost:8555`.

---

## Interactive Launcher (Alternative)

**Linux / macOS:**
```bash
./start.sh
```

**Windows:**
```bat
start.bat
```

---

## Usage

### Monitor API

All endpoints return JSON.

**Liveness:**
```bash
curl http://localhost:8011/health/
```

**System statistics** (requires `X-Monitor-Key` if `MONITOR_API_KEY` is set):
```bash
curl -H "X-Monitor-Key: <your-key>" http://localhost:8011/monitor/stats
```

**Recent credentials:**
```bash
curl -H "X-Monitor-Key: <your-key>" "http://localhost:8011/monitor/credentials?limit=10"
```

**Recent exfiltrated messages:**
```bash
curl -H "X-Monitor-Key: <your-key>" "http://localhost:8011/monitor/messages?limit=20"
```

**Detailed health check (DB + Redis + Bot API):**
```bash
curl -H "X-Monitor-Key: <your-key>" http://localhost:8011/health/detailed
```

**Queue depths + oldest job age** (added by reliability rebuild):
```bash
curl -H "X-Monitor-Key: <your-key>" http://localhost:8011/health/queues
```
Returns per-queue `{length, oldest_job_age_seconds, oldest_enqueued_at}` for `celery`, `scrape`, `scanners`, `validation`.

**Captured webhook URLs** — bots where a third party has registered a webhook (potential C2 / researcher endpoints):
```bash
curl -H "X-Monitor-Key: <your-key>" "http://localhost:8011/monitor/webhooks?limit=100"
```

**Circuit breaker status:**
```bash
curl -H "X-Monitor-Key: <your-key>" http://localhost:8011/health/circuit-breakers
```

**Force-reset a circuit breaker:**
```bash
curl -X POST -H "X-Monitor-Key: <your-key>" http://localhost:8011/health/circuit-breakers/shodan/reset
```

**Manually trigger a scanner** (development only  returns 403 in production):
```bash
curl -X POST http://localhost:8011/scan/trigger \
  -H "Content-Type: application/json" \
  -d '{"source": "shodan", "query": "telegram bot"}'
```

Valid `source` values: `shodan`, `fofa`, `github`, `gitlab`, `urlscan`.

**Ingest credentials from external tooling:**
```bash
curl -X POST http://localhost:8011/ingest/extension/credentials \
  -H "Content-Type: application/json" \
  -d '{"source":"manual","results":[{"token":"123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}]}'
```

### Telegram Admin Commands

Send these commands in the monitor supergroup from a whitelisted admin account:

| Command | Effect |
|---|---|
| `/status` | System health, pending counts, bot pool info |
| `/pause` | Pause scanners and broadcaster |
| `/resume` | Resume all operations |
| `/bots` | Show bot pool status and lock state |
| `/starthunter` | Start interactive Telegram account login |
| `/restart` | Restart the bot listener process |
| `/help` | Full command reference |

### CSV Import

Drop `.csv` files into the `imports/` directory (mounted as a Docker volume). The `system.import_csv` task picks them up every 5 minutes.

Required format:
```csv
token,chat_id
1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx,-1001234567890
9876543210:AAyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy,
```

The `chat_id` column is optional  leave blank if unknown.

### Docker Compose Operations

```bash
# Start all services (build if needed)
docker compose up -d --build

# Tail logs  all services
docker compose logs -f

# Tail logs  specific service
docker compose logs -f worker-scrape

# Stop services (preserve volumes)
docker compose down

# Stop and wipe all volumes (full reset)
docker compose down -v

# Rebuild after code changes
docker compose build && docker compose up -d
```

---

## Testing

### Install test dependencies

```bash
pip install -r requirements-dev.txt
```

### Run the full test suite

```bash
pytest
```

**176 tests** (168 unit + 5 integration + 3 top-level) across unit, integration, API, security, and Supabase R/W suites — plus scrape classification, broadcast retry accounting, queue monitor, canary probe, and Telethon lifecycle coverage added by the reliability rebuild.

### Run specific suites

```bash
# Unit tests only (no external dependencies)
pytest tests/unit/

# API tests
pytest tests/test_api.py

# Security tests
pytest tests/test_security.py

# Integration tests (requires live Supabase + Redis)
pytest tests/integration/

# Supabase read/write test (writes a real record)
ALLOW_SUPABASE_WRITE=1 pytest tests/test_supabase_rw.py

# With coverage report
pytest --cov=app --cov-report=html
```

### Test markers

```
@pytest.mark.unit         Unit tests (no external dependencies)
@pytest.mark.integration  Integration tests (may require live services)
@pytest.mark.slow         Long-running tests
```

---

## Development

### Code quality

```bash
# Lint and auto-fix
ruff check app/ --fix

# Format
ruff format app/

# Type check
mypy app/
```

### Pre-commit hooks

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

### Run the API locally (no Docker)

```bash
export $(grep -v '^#' .env | xargs)
uvicorn app.api.main:app --reload --port 8001
```

### Run the frontend locally

```bash
cd frontend
npm install
npm run dev
```

Frontend requires two environment variables in `frontend/.env.local`:
```
NEXT_PUBLIC_SUPABASE_URL=https://<ref>.supabase.co
NEXT_PUBLIC_SUPABASE_KEY=<anon-key>
```

### Chrome Extension

1. Open Chrome  `chrome://extensions/`
2. Enable **Developer mode**
3. Click **Load unpacked**  select the `extension/` directory
4. Open the extension popup and configure:
   - **Supabase URL** and **Anon Key** (for direct write fallback)
   - **Write Secret** (must match `app.extension_write_secret` in your Supabase DB)
   - **API URL** (recommended  e.g. `http://localhost:8011`) for server-side encryption

Recommended unpaid FOFA workflow:

1. Use the FOFA website or your FOFA scraping extension to collect candidate hits.
2. Let this Chrome extension capture the tokens from those FOFA pages.
3. Set **API URL** so the extension sends findings to `/ingest/extension/credentials`; that keeps token encryption on the server side.
4. Leave `FOFA_EMAIL` and `FOFA_KEY` empty unless you later decide to pay for FOFA API access.

Notes:
- The FOFA API scanner is optional and only applies when `FOFA_EMAIL` and `FOFA_KEY` are configured.
- The extension path works without FOFA API credentials.
- Direct Supabase writes are still available as a fallback, but the API route is the safer default.

---

## Project Structure

```
.
 app/
    api/
       main.py                  FastAPI app, lifespan hooks, CORS
       routers/
           health.py            /health/* endpoints
           monitor.py           /monitor/* endpoints
           scan.py              /scan/trigger (dev only)
           ingest.py            /ingest/extension/credentials
    core/
       config.py                Pydantic Settings, env validation
       database.py              Supabase client singleton
       security.py              Fernet encrypt/decrypt
       redis_srv.py             Locks, cooldowns, counters
       retry.py                 @retry decorator (sync/async)
       circuit_breaker.py       Per-service circuit breakers
       metrics.py               In-memory metrics collector
       audit.py                 Security audit event logger
       constants.py             Application-wide constants
       logger.py                Logger factory
    schemas/
       models.py                Pydantic request/response models
    services/
       scanners.py              ShodanService, FofaService, UrlScanService,
                                  GithubService, GitlabService, SerperService
       scanners_extension.py    GithubGistService, GrepAppService,
                                  PublicWwwService, BitbucketService,
                                  PastebinService, GoogleSearchService,
                                  NetlasService
       scraper_srv.py           Telethon chat history scraper (4 strategies)
       broadcaster_srv.py       Telegram message sender, topic manager
       bot_manager_srv.py       Telethon client pool (BotClientManager)
       bot_listener.py          Admin command handler, watchdog
       user_agent_srv.py        User session manager (multi-session rotation)
    utils/
       helpers.py               Token/chat ID validation & extraction
    workers/
        celery_app.py            Celery config, persistent event loop,
                                   beat schedule (25 tasks)
        tasks/
            flow_tasks.py        enrich, exfiltrate, broadcast, rescrape,
                                   heartbeat, help, broadcaster singleton
            scanner_tasks.py     Per-scanner task runners, _save_credentials_async
            audit_tasks.py       audit_active_topics, self_heal,
                                   enforce_whitelist, cleanup_general_topic
            import_tasks.py      system.import_csv  CSV file pipeline
 database/
    init.sql                     Schema DDL (idempotent, IF NOT EXISTS)
    rls_policies.sql             Row Level Security policies
 extension/                       Manifest V3 Chrome extension
    manifest.json
    background.js                Service worker  scan logic, upload
    content.js                   FOFA page scraper
    ui/                          Popup HTML/JS/CSS
 frontend/                        Next.js 16 dashboard (optional)
 imports/                         Drop CSV files here for auto-import
 tests/
    conftest.py                  Fixtures, env injection
    test_api.py                  API route tests
    test_security.py             Encryption tests
    test_scraper_restriction.py  Scraper caching tests
    test_supabase_rw.py          Live DB read/write test
    unit/                        Isolated unit tests (55 tests)
    integration/                 Integration tests (5 tests)
 scripts/
    validate_deployment.py       Post-deploy health checks
    validate_startup.py          Pre-start environment checks
 .env.template                    Environment variable template
 docker-compose.yml               10-service stack definition
 Dockerfile                       python:3.11-slim-bookworm, non-root user
 docker-entrypoint.sh             Container entrypoint  CSV pre-processing
```

---

## Security Notes

- **`SUPABASE_SERVICE_ROLE_KEY`** bypasses all Row Level Security. Never expose it to browser clients or commit it to version control.
- **`ENCRYPTION_KEY`** is the sole protection for stored tokens. Losing it makes all stored credentials unrecoverable.
- **`MONITOR_API_KEY`** should be set in production to protect monitoring endpoints.
- Set `ENV=production` to disable OpenAPI docs and the manual scan endpoint.
- The stack does not terminate TLS. Place it behind a reverse proxy (nginx, Caddy) for external exposure.
- The `EXTENSION_WRITE_SECRET` is stored only inside the Supabase database  never in source code or environment files.

---

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Advanced Features (added 2026-08-03 → 2026-08-06)

### Webhook Recon + Takeover Pipeline

The system passively fingerprints third-party webhooks registered on captured bots:

```bash
# View captured webhook URLs + probe results (TLS, Shodan, web recon)
curl -H "X-Monitor-Key: <key>" http://localhost:8011/monitor/webhooks

# View C2 operator clusters (who controls the most bots)
curl -H "X-Monitor-Key: <key>" http://localhost:8011/monitor/operators

# Force immediate takeover of all webhook-registered bots
docker exec theprawnhunter_worker-core celery -A app.workers.celery_app call flow.force_webhook_takeover_pass
```

When `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE=True`, the system:
1. Detects third-party webhooks via `getWebhookInfo`
2. Deletes them via `deleteWebhook`
3. Registers our honeypot webhook (if `HONEYPOT_MODE=True`)
4. Polls via `getUpdates` for any queued messages

### Honeypot Mode (Push Receiver)

After takeover, optionally registers OUR webhook so Telegram pushes messages to us in real-time.

**Requirements:**
- Public HTTPS endpoint (Cloudflare Tunnel recommended — free, no port forwarding)
- `HONEYPOT_MODE=True`
- `HONEYPOT_WEBHOOK_URL=https://your-public-domain/honeypot`
- `HONEYPOT_SECRET=<random 32+ char string>`
- `HONEYPOT_ALLOWLIST=AUTO` (all bots) or comma-separated UUIDs

**Setup with Cloudflare Tunnel:**
```powershell
# One-time: authenticate with Cloudflare
cloudflared tunnel login

# Create tunnel + route DNS (use scripts/setup_cloudflare_tunnel.ps1 for full automation)
cloudflared tunnel create prawnhunter
cloudflared tunnel route dns prawnhunter your-subdomain.your-domain.com

# Run tunnel (or install as Windows service)
cloudflared tunnel run prawnhunter
```

**Endpoints:**
- `POST /honeypot/receive/{credential_id}` — Telegram webhook receiver (auth via `X-Telegram-Bot-Api-Secret-Token` header)
- `GET /honeypot/status` — configuration state (requires monitor key)

**Captured data stored in `honeypot_updates` table** — full Telegram update JSON including sender user_id, message text, media file_ids.

### Honeypot Redirect Injection

When the honeypot captures a user's message, automatically replies FROM the captured bot directing them to your onboard bot:

```env
HONEYPOT_REDIRECT_MODE=True
HONEYPOT_REDIRECT_BOT=bryanseahbot
HONEYPOT_REDIRECT_DEEPLINK=migrate
```

The message sent:
```
⚠️ This service has been migrated.
Your request could not be processed here.
To continue, use the updated channel:
👉 https://t.me/bryanseahbot?start=migrate
This is an automated notification.
```

Per-user dedup (permanent Redis key) ensures each user only receives the redirect once per bot.

### Cloudflare WAF Rules

When exposing the API via Cloudflare Tunnel, 4 WAF rules protect the endpoint:

1. **Block bot scanners** — nmap, masscan, nuclei, gobuster, sqlmap, empty UA
2. **Challenge suspicious paths** — `../`, `/admin`, `/wp-*`, `/.env`, `/.git`, `/phpmyadmin`
3. **Honeypot IP restriction** — only Telegram's IP ranges (149.154.160.0/20, 91.108.x.0/22) can POST to `/honeypot/receive`
4. **Block all non-allowed traffic** — only honeypot POSTs, monitor-key requests, root + health GETs pass

### Attribution Graph

Links Telegram user_ids across multiple captured bots to identify serial victims:

```bash
# Manual trigger
docker exec theprawnhunter_worker-core celery -A app.workers.celery_app call flow.attribution_graph_report
```

Runs weekly (Tuesday 08:00 UTC). Requires `sender_user_id` column (populated from new scrapes).

### Full-Text Search

```bash
# Search across 283k+ exfiltrated messages (pg_trgm indexed)
curl -H "X-Monitor-Key: <key>" "http://localhost:8011/monitor/search?q=bitcoin&limit=50"
```

Supports `media_only=true` and `since_hours=24` filters.

### Media Forensics

Automatically SHA-256 + perceptual-hashes photos from exfiltrated messages to detect the same image being sent across multiple compromised bots (common operator fingerprint).

```bash
# Manual trigger
docker exec theprawnhunter_worker-core celery -A app.workers.celery_app call flow.hash_exfil_media
docker exec theprawnhunter_worker-core celery -A app.workers.celery_app call flow.media_duplicate_report
```

### FOFA Extension Automation

The Chrome extension scrapes FOFA for exposed Telegram bot tokens. To run autonomously:

1. Open Chrome with the CDP debug profile:
   ```powershell
   Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" -ArgumentList "--remote-debugging-port=9222","--no-first-run","--user-data-dir=$env:TEMP\chrome_fofa_puppeteer","https://en.fofa.info/"
   ```

2. Load the extension via `chrome://extensions` → "Load unpacked" → `extension/` folder

3. Log into FOFA in that Chrome window (one-time)

4. Trigger scan via CDP:
   ```powershell
   # The content script bridges postMessage to the background service worker
   # Use any CDP tool to evaluate on the FOFA page:
   window.postMessage({type:'TH_START_SCAN', query:'body="api.telegram.org/bot"', domain:'en.fofa.info', mode:'both'}, '*')
   ```

The scan runs through 49 countries × 2 domains, auto-uploads every 10 countries.

### Fernet Key Rotation

```bash
# 1. Generate new key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Move current ENCRYPTION_KEY to ENCRYPTION_KEY_LEGACY in .env
# 3. Set ENCRYPTION_KEY to the new key
# 4. Restart workers
# 5. Run rotation script
docker exec theprawnhunter_worker-core python scripts/rotate_credentials.py --batch-size 100
```

### Security Hardening

All ports bound to `127.0.0.1` (Redis, Frontend, Flower, API). External access only via Cloudflare Tunnel.

- **Flower dashboard**: requires `FLOWER_BASIC_AUTH` (refuses to start with default)
- **Rate limiting**: slowapi 120 req/min per key (Redis-backed, cross-worker)
- **Bot admin gate**: `/starthunter` requires `ALLOW_PUBLIC_STARTHUNTER=True` or whitelisted admin
- **Token redaction**: MultiFernet + broadened regex catches tokens in logs/audit
- **Session management**: re-login auto-cleans old files, membership audit every 30min

<!-- repo renamed to theprawnhunter on 2026-08-04 -->
