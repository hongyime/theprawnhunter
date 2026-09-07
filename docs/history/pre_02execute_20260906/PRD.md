# Product Requirements Document

> **Source of truth**: This document reflects the codebase as implemented. Every claim is grounded in source files.

---

## 1. Executive Summary

This system is a self-hosted, continuously-running OSINT pipeline that automatically discovers exposed bot tokens across 13 public data sources, validates each token against the live messaging API, harvests full message history from every accessible chat, and broadcasts findings into a private supergroup organized by per-bot forum topics.

**Runtime stack:** Python 3.11, FastAPI, Celery, Redis, Telethon, python-telegram-bot  
**Database:** Supabase (managed PostgreSQL) with Row Level Security  
**Frontend:** Next.js 16 + React 19 (optional read-only dashboard)  
**Browser Extension:** Manifest V3 Chrome extension (FOFA scraper + direct ingest)  
**Deployment:** Docker Compose  7 services

---

## 2. System Architecture

### 2.1 Component Topology

```
[Chrome Extension]
        POST /ingest/extension/credentials  (preferred  server-side encryption)
        OR direct Supabase REST write        (fallback  raw token, self-healed)
      
[FastAPI API  2 uvicorn workers]
       /health/*
       /monitor/*
       /scan/trigger
       /ingest/*

[Celery Beat]  schedules 25 tasks
      
       worker-core     (queue: celery,    concurrency: 4)
           flow_tasks.py, audit_tasks.py, import_tasks.py
      
       worker-scanners (queue: scanners,  concurrency: 2)
           scanner_tasks.py
      
       worker-scrape   (queue: scrape,    concurrency: 2)
            flow_tasks.py (exfiltrate_chat, rescrape_active)

[Bot Service]  bot_listener.py (admin commands, watchdog)

[Supabase PostgreSQL]  service-role key (all workers/API)
[Redis 7-alpine]       broker, result backend, locks, cooldowns, counters
```

### 2.2 Docker Services

| Service | Image | Command | Ports | Restart |
|---|---|---|---|---|
| `redis` | redis:7-alpine | default | `${REDIS_PORT:-6379}:6379` | always |
| `api` | python:3.11-slim-bookworm (built) | `uvicorn app.api.main:app --workers 2` | `${API_PORT:-8011}:8001` | always |
| `worker-core` | built | `celery worker -Q celery --concurrency=4` |  | always |
| `worker-scanners` | built | `celery worker -Q scanners --concurrency=2` |  | always |
| `worker-scrape` | built | `celery worker -Q scrape --concurrency=2` |  | always |
| `beat` | built | `celery beat` |  | always |
| `bot` | built | `python -m app.services.bot_listener` |  | always |

**Named volumes:** `redis_data`, `sessions`, `imports`  
**Log driver:** json-file, 10 MB max, 3 rotations per service  
**Non-root user:** all containers run as uid 1000 (`celery`)

### 2.3 Data Flow Pipeline

```
[Scanner Sources x13]
          regex extraction + format validation
        
[Token Validation]   GET /getMe  External Bot API
          live token confirmed
        
[Persistence]   Fernet encrypt  discovered_credentials (status=pending)
        
          flow.enrich_credential
[Enrichment]   get_dialogs  all chats enumerated
                 create forum topic  Monitor Supergroup
          status=active
[Exfiltration]  (4 strategies)
                 upsert  exfiltrated_messages
        
[Broadcasting]   atomic DB claim  post to topic  Monitor Supergroup
                 mark is_broadcasted=true
        
[Self-Healing]   hourly/6h audits  reconcile DB vs live state
```

---

## 3. Feature Matrix

### 3.1 Token Discovery  13 Scanner Sources

| Scanner Class | API Target | Auth Required | Schedule | Queue |
|---|---|---|---|---|
| `ShodanService` | Shodan Internet DB | `SHODAN_KEY` | Every 4 h @ :20 | scanners |
| `FofaService` | FOFA search engine | `FOFA_EMAIL` + `FOFA_KEY` | Every 4 h @ :00 (+1 h offset) | scanners |
| `UrlScanService` | URLScan.io | `URLSCAN_KEY` | Every 4 h @ :40 | scanners |
| `GithubService` | GitHub Code Search API v3 | `GITHUB_TOKEN` | Every 4 h @ :00 | scanners |
| `GithubGistService` | GitHub Public Gists API | `GITHUB_TOKEN` | Every 6 h @ :45 | scanners |
| `GitlabService` | GitLab Blobs Search API | `GITLAB_TOKEN` | Every 6 h @ :10 | scanners |
| `GrepAppService` | grep.app regex search | None | Every 6 h @ :25 | scanners |
| `PublicWwwService` | PublicWWW.com | `PUBLICWWW_KEY` | Every 6 h (via scan_publicwww) | scanners |
| `PastebinService` | Pastebin scraping API | None (IP whitelist) | Every 12 h @ :15 | scanners |
| `SerperService` | Serper.dev (Google SERPs) | `SERPER_API_KEY` | Every 12 h @ :35 | scanners |
| `GoogleSearchService` | Google Custom Search API | `GOOGLE_SEARCH_KEY` + `GOOGLE_CSE_ID` | Every 12 h @ :50 | scanners |
| `BitbucketService` | Bitbucket workspace code search | `BITBUCKET_API_TOKEN` | Every 8 h @ :30 | scanners |
| `NetlasService` | Netlas.io response search | `NETLAS_API_KEY_1` / `NETLAS_API_KEY_2` | Daily @ 03:00 UTC | scanners |

**Additional dedicated task:** `scanner.scan_shodan_c2`  Shodan queries targeting C2/RAT infrastructure patterns. Runs every 6 h @ :10.

All scanners degrade gracefully when API keys are absent  missing-key scanners return empty results and log a warning.

### 3.2 Token Validation

- **Regex pattern:** `\b(\d{8,15}:[A-Za-z0-9_-]{35})\b`
- **Strict rejection rules (applied in `_is_valid_token()` and `is_valid_telegram_token()`):**
  - Fernet ciphertexts (secret starts with `gAAAA`)
  - Pure hexadecimal strings
  - Bot ID with leading zeros
  - Secret not exactly 35 characters
  - Bot ID length outside 8-15 digits range
- **Liveness check:** HTTP `GET /getMe` against the external bot API
- **Deduplication:** SHA-256 hash of plaintext token; `token_hash` column is `UNIQUE`

### 3.3 Credential Persistence & Encryption

- **Algorithm:** Fernet (AES-128-CBC + HMAC-SHA256) via `cryptography==46.0.7`
- **Key:** 44-character URL-safe base64 string, validated at startup by Pydantic field validator
- **Storage:** `bot_token` column always contains Fernet ciphertext
- **Self-healing:** any plaintext token encountered during enrichment or exfiltration is automatically re-encrypted in place before use

### 3.4 Chat Enumeration (Enrichment  `flow.enrich_credential`)

- Telethon `get_dialogs()` retrieves all chats accessible to the bot
- Creates a forum topic in the monitor supergroup named `@{username} / {bot_id}`
- Stores `topic_id`, `bot_username`, `bot_id`, `all_chats` in the `meta` JSONB column
- Credential status advances from `pending`  `active`

### 3.5 Message Exfiltration  4 Strategies (`flow.exfiltrate_chat`)

| Strategy | Method | When Used |
|---|---|---|
| 1  Direct history | Telethon `iter_messages()` | Primary |
| 2  ID bruteforce | Telethon `get_messages()` with explicit ID list | When strategy 1 is restricted |
| 3  Bot API updates | `getUpdates` (recent messages only) | Fallback; provides anchor ID |
| 4  Blind forwarding | Auto-invite bot; forward from target chat to monitor group topic | Last resort |

All strategies upsert into `exfiltrated_messages` with unique constraint on `(credential_id, telegram_msg_id)`.

### 3.6 Message Broadcasting (`flow.broadcast_pending`)

- **Distributed Redis lock:** `telegram_hunter:lock:broadcast`, TTL 55 s
- **Atomic DB claim:** `broadcast_claimed_at` conditional UPDATE  only one worker wins per message
- **Stale claim reclamation:** claims older than 5 minutes become eligible for re-claim
- **Batch size:** 100 messages per run
- **Rate limiting:** 2-second sleep between messages
- **Multi-bot rotation:** round-robin across `MONITOR_BOT_TOKEN` list via `itertools.cycle` singleton
- **Topic recreation:** if topic is deleted, it is recreated and the DB is updated before retry

### 3.7 CSV Import Pipeline (`system.import_csv`)

- Scans `/app/imports/` for `.csv` files every 5 minutes
- Atomically claims each file by renaming to `.pending`
- Parses `token` and `chat_id` columns (header row required)
- Passes results through the same `_save_credentials_async` validation path as scanners
- Moves processed files to `/app/imports/processed/`
- Supported format:
  ```
  token,chat_id
  1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx,-1001234567890
  ```

### 3.8 Scheduled Maintenance Tasks

| Task Name | Schedule | Purpose |
|---|---|---|
| `flow.broadcast_pending` | Every `BROADCAST_INTERVAL_MINUTES` (default 60) | Post unbroadcasted messages |
| `flow.rescrape_active` | Every `RESCRAPE_INTERVAL_HOURS` (default 1) | Re-pull history from active chats |
| `flow.system_heartbeat` | Every 30 min | Redis timestamp + Telegram ping |
| `flow.system_help` | Every 6 h @ :30 | Post command reference to monitor group |
| `audit.audit_active_topics` | Every `AUDIT_INTERVAL_HOURS` (default 1) @ :15 | Verify topic exists; trigger re-enrichment if deleted |
| `system.self_heal` | Every 6 h @ :45 | Reconcile DB credentials vs live topic state |
| `system.enforce_whitelist` | Every 6 h @ :00 (+1 h offset) | Ensure whitelisted bots are present and admin |
| `system.cleanup_general_topic` | Every 1 h @ :30 | Delete system log messages older than 12 h |
| `system.import_csv` | Every 5 min | Process CSV files in `/app/imports/` |
| `scanner.retry_cold` | Every 12 h @ :50 | Retry tokens that previously failed enrichment |

### 3.9 Admin Bot Commands

Commands accepted only from whitelisted admins or the anonymous group admin (`ANONYMOUS_ADMIN_ID`).

| Command | Handler | Effect |
|---|---|---|
| `/start` | `start()` | Greet user, confirm availability |
| `/help` | `help_command()` | Display command reference with bot pool list |
| `/commands` | `help_command()` | Alias for `/help` |
| `/status` | `status()` | Redis state, pending broadcast count, bot pool info |
| `/pause` | `pause()` | Set `system:paused` Redis key  scanners and broadcaster skip |
| `/resume` | `resume()` | Delete `system:paused` Redis key |
| `/restart` | `restart()` | Set stop event  process exits and Docker restarts it |
| `/bots` | `bots_command()` | Show bot pool with lock status per bot |
| `/starthunter` | `starthunter()` | Interactive Telethon account login flow (ConversationHandler) |

### 3.10 HTTP Monitoring API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | None | Liveness; returns `{"status":"ok"}` or `{"status":"active"}` in production |
| GET | `/health/` | None | Basic health  always 200 |
| GET | `/health/detailed` | `X-Monitor-Key` | DB + Redis + Bot API reachability; 503 if degraded |
| GET | `/health/metrics` | `X-Monitor-Key` | In-memory `MetricsCollector` counters |
| GET | `/health/circuit-breakers` | `X-Monitor-Key` | State of all 4 named circuit breakers |
| POST | `/health/circuit-breakers/{service}/reset` | `X-Monitor-Key` | Force-reset a named breaker |
| GET | `/monitor/stats` | `X-Monitor-Key` | Aggregate credential/message counts |
| GET | `/monitor/credentials?limit=N` | `X-Monitor-Key` | Recent credentials (default N=100) |
| GET | `/monitor/messages?limit=N` | `X-Monitor-Key` | Recent exfiltrated messages (default N=100) |
| POST | `/scan/trigger` | None (dev only) | Enqueue scanner task; **403 in production** |
| POST | `/ingest/extension/credentials` | None | Bulk credential ingest from extension (server-side encryption) |

OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`) disabled when `ENV=production`.

### 3.11 Chrome Extension

- **Manifest version:** 3
- **Function:** Automates FOFA search across 49 country codes, extracts bot tokens from page source, validates each token via the external bot API, then uploads results
- **Upload modes:**
  1. **API route (preferred):** POST to `/ingest/extension/credentials`  tokens encrypted server-side
  2. **Direct Supabase write (fallback):** REST insert with `x-extension-secret` header  raw token stored, self-healed by backend
- **Config fields (stored in `chrome.storage.sync`):** Supabase URL, Supabase anon key, Write Secret, API URL
- **Watchdog alarm:** fires every 2 minutes to unstick stalled scans

### 3.12 Frontend Dashboard (Optional)

- **Framework:** Next.js 16.2.4, React 19.2.3, TypeScript 5, Tailwind CSS 4
- **Supabase client:** `@supabase/supabase-js ^2.89.0` with anon key (RLS-restricted)
- **Sidebar:** queries `discovered_credentials_public` view (safe projection  no token/hash) for credentials that have associated messages; real-time INSERT subscription
- **ChatWindow:** queries `exfiltrated_messages` for selected credential; real-time INSERT subscription; read-only display
- **Mobile:** renders a redirect notice  desktop only
- **Environment variables required:** `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_KEY`

---

## 4. Data Architecture

### 4.1 Database Schema

**`discovered_credentials`**

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK, `gen_random_uuid()` |  |
| `bot_token` | TEXT | NOT NULL | Fernet-encrypted ciphertext |
| `token_hash` | TEXT | NOT NULL, UNIQUE | SHA-256 of plaintext token |
| `chat_id` | BIGINT |  | Primary chat from enrichment |
| `bot_id` | TEXT |  | Numeric bot ID as string |
| `bot_username` | TEXT |  | `@username` |
| `chat_name` | TEXT |  | Display name of primary chat |
| `chat_type` | TEXT |  | `group`, `supergroup`, `channel`, `private` |
| `source` | TEXT |  | Scanner name |
| `status` | TEXT | CHECK (`pending`, `active`, `revoked`) | Default `pending` |
| `meta` | JSONB | Default `{}` | `topic_id`, `all_chats`, `bot_id`, `bot_username`, etc. |
| `created_at` | TIMESTAMPTZ | Default NOW() |  |
| `updated_at` | TIMESTAMPTZ | Default NOW() |  |

Indexes: `idx_creds_status` on `status`; `idx_creds_bot_id` on `bot_id`

---

**`exfiltrated_messages`**

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK |  |
| `credential_id` | UUID | FK  `discovered_credentials.id` ON DELETE CASCADE |  |
| `telegram_msg_id` | INT | NOT NULL | Telegram-assigned message ID |
| `sender_name` | TEXT |  | Message author display name |
| `content` | TEXT |  | Message body |
| `media_type` | TEXT | Default `text` | `text`, `photo`, `document`, `other` |
| `file_meta` | JSONB | Default `{}` | `mime`, `size`, `id`, etc. |
| `is_broadcasted` | BOOLEAN | Default FALSE | Set TRUE after successful broadcast |
| `broadcast_claimed_at` | TIMESTAMPTZ | Default NULL | Distributed claim timestamp |
| `created_at` | TIMESTAMPTZ | Default NOW() |  |

Unique constraint: `unique_msg_per_credential` on `(credential_id, telegram_msg_id)`  
Indexes: `idx_messages_credential_id`; partial `idx_messages_is_broadcasted` WHERE `is_broadcasted = FALSE`; composite `idx_messages_claimed` on `(is_broadcasted, broadcast_claimed_at)`

---

**`telegram_accounts`**

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK |  |
| `phone` | TEXT | NOT NULL, UNIQUE | Telegram account phone number |
| `session_path` | TEXT | NOT NULL | Absolute path to `.session` file |
| `status` | TEXT | CHECK (`active`, `inactive`) | Default `active` |
| `locked_by` | TEXT |  | `{hostname}:{pid}` of current holder |
| `locked_until` | TIMESTAMPTZ |  | Session lease expiry (10 min) |
| `created_at` | TIMESTAMPTZ | Default NOW() |  |
| `updated_at` | TIMESTAMPTZ | Default NOW() |  |

Indexes: `idx_accounts_phone`; `idx_accounts_status`

---

**`audit_logs`**

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK |  |
| `timestamp` | TIMESTAMPTZ | Default NOW() |  |
| `event_type` | TEXT | NOT NULL | `token_decrypted`, `credential_created`, `token_revoked`, etc. |
| `credential_id` | UUID | FK  `discovered_credentials.id` ON DELETE SET NULL |  |
| `user_agent` | TEXT | Default `system` |  |
| `success` | BOOLEAN | Default TRUE |  |
| `details` | JSONB | Default `{}` |  |

Indexes: `idx_audit_event_type`; `idx_audit_timestamp`

---

### 4.2 Views

**`discovered_credentials_public`**  safe projection for anonymous (frontend/extension) reads.  
Exposes only: `id`, `created_at`, `source`, `status`, `meta`.  
Hides: `bot_token`, `token_hash`, `bot_id`, `bot_username`, `chat_id`, `chat_name`, `chat_type`.  
`GRANT SELECT ON discovered_credentials_public TO anon;`

### 4.3 Row Level Security

| Table | anon SELECT | anon INSERT | anon UPDATE | anon DELETE |
|---|---|---|---|---|
| `discovered_credentials` | Denied (raw table) | Allowed with valid `x-extension-secret` header | Allowed with valid `x-extension-secret` header | Denied |
| `discovered_credentials_public` (view) | Allowed |  |  |  |
| `exfiltrated_messages` | Allowed | Denied | Denied | Denied |
| `telegram_accounts` | Denied | Denied | Denied | Denied |
| `audit_logs` | Not exposed to anon |  |  |  |

Service-role key bypasses all RLS. Used exclusively by backend workers and API.

The `x-extension-secret` is stored as a PostgreSQL database parameter (`app.extension_write_secret`) set via `ALTER DATABASE postgres SET app.extension_write_secret = '...'`. It is never stored in application code or environment variables.

### 4.4 Pydantic API Models

```python
class CredentialOut(BaseModel):
    id: UUID
    source: str
    status: str
    chat_id: Optional[int]
    meta: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

class MessageOut(BaseModel):
    id: UUID
    credential_id: UUID
    telegram_msg_id: int
    sender_name: Optional[str]
    content: Optional[str]
    media_type: str
    is_broadcasted: bool
    created_at: datetime

class StatsOut(BaseModel):
    credentials_total: int
    credentials_active: int
    messages_exfiltrated: int
    messages_broadcasted: int

class ScanRequest(BaseModel):
    source: str  # shodan | fofa | github | gitlab | urlscan
    query: str

class ExtensionIngestRequest(BaseModel):
    source: str = "extension"
    domain: Optional[str]
    query: Optional[str]
    results: list[ExtensionCredential]

class ExtensionIngestResponse(BaseModel):
    inserted: int
    updated: int
    skipped: int
```

---

## 5. Security Architecture

### 5.1 Credential Encryption

- **Algorithm:** Fernet (AES-128-CBC + HMAC-SHA256)
- **Key generation:** `Fernet.generate_key()`  44-character URL-safe base64
- **Validation:** key length strictly enforced at startup via `@field_validator('ENCRYPTION_KEY')`
- **Tokens are never logged** and never stored in plaintext after the self-heal path runs

### 5.2 Database Access Tiers

| Tier | Key Used | Access Level |
|---|---|---|
| Backend (all workers, API) | `SUPABASE_SERVICE_ROLE_KEY` | Full bypass of RLS |
| Frontend (Next.js) | `SUPABASE_KEY` (anon key) | RLS-restricted; public view + messages read only |
| Chrome Extension (direct write) | `SUPABASE_KEY` (anon key) + `x-extension-secret` | INSERT/UPDATE on `discovered_credentials` only |

### 5.3 API Authentication

- `MONITOR_API_KEY` environment variable enables header authentication
- Protected endpoints require `X-Monitor-Key: <value>` header
- If `MONITOR_API_KEY` is unset, protected endpoints are openly accessible (development behaviour)
- Auth check implemented via `_check_monitor_auth()` helper in `monitor.py`; consistent pattern across all health and monitor routes

### 5.4 CORS Policy

`allow_origins=["*"]`, `allow_credentials=False`. The API relies on `MONITOR_API_KEY` for sensitive endpoint protection, not CORS.

### 5.5 Production Hardening (`ENV=production`)

- OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`) disabled
- `POST /scan/trigger` returns 403
- `GET /` returns `{"status":"active"}` only

### 5.6 Session File Security

- Session files stored in `/app/sessions/` with permissions `0o600` (owner read/write only)
- Temporary session copies written to `/tmp/{session_name}.session` during use; cleaned up after disconnect
- Session files never committed to version control (`.gitignore` covers `*.session`)

### 5.7 Pinned Dependencies (Security-Relevant)

| Package | Pinned Version | Addressed CVEs |
|---|---|---|
| `fastapi` | 0.136.0 | CVE-2024-47874, CVE-2025-54121 |
| `httpx` | 0.28.1 | CVE-2024-37891 (SSRF) |
| `cryptography` | 46.0.7 | CVE-2024-12797, CVE-2026-26007, CVE-2026-34073 |
| `requests` | 2.33.1 | CVE-2024-47081, CVE-2026-25645 |

---

## 6. Reliability & Resilience

### 6.1 Retry Strategy (`app/core/retry.py`)

`@retry` decorator  supports both sync and async functions:

| Parameter | Default | Description |
|---|---|---|
| `max_attempts` | 3 | Total attempts including first |
| `base_delay` | 1.0 s | Initial backoff delay |
| `max_delay` | 60.0 s | Backoff cap |
| `exponential` | True | `delay = base  2^(attempt-1)` |
| `exceptions` | `(Exception,)` | Exception types to catch |

Specialised variants: `retry_on_telegram_error()` (catches `RetryAfter`, `TimedOut`, `NetworkError`; 230 s backoff) and `retry_on_connection_error()` (catches `ConnectionError`, `TimeoutError`, `httpx.ConnectError`; 110 s backoff).

### 6.2 Circuit Breaker Pattern (`app/core/circuit_breaker.py`)

Four named breakers: `shodan`, `urlscan`, `github`, `fofa`.

| Parameter | Value |
|---|---|
| `failure_threshold` | 5 consecutive failures  OPEN |
| `recovery_timeout` | 60 s  HALF_OPEN |
| `success_threshold` | 2 consecutive successes  CLOSED |

States exposed at `GET /health/circuit-breakers`. Manual reset via `POST /health/circuit-breakers/{service}/reset`.

### 6.3 Distributed Locking (Redis)

| Lock Key | TTL | Purpose |
|---|---|---|
| `telegram_hunter:lock:broadcast` | 55 s | Single broadcaster at a time |
| `bot_listener:poll_lock:{bot_id}` | 120 s | Single `getUpdates` poller per bot |
| `user_agent:{session_name}` | 600 s | Exclusive Telethon session use |
| `enrich_requeue:{credential_id}` | 3600 s | Cooldown  prevents unbounded enrichment re-queuing |

### 6.4 Persistent Event Loop

Each Celery worker process maintains a single `asyncio` event loop via `get_worker_loop()` (exported from `celery_app.py`). All tasks call `get_worker_loop().run_until_complete(coro)` instead of `asyncio.run()`. This preserves `asyncio.Lock` state across task invocations and keeps Telethon connections alive in `BotClientManager`.

### 6.5 Task Reliability Settings

| Setting | Value |
|---|---|
| `task_acks_late` | True  ack only after completion |
| `worker_prefetch_multiplier` | 1  one task at a time |
| `worker_max_memory_per_child` | 800 MB  auto-recycle |
| `task_soft_time_limit` | 1200 s (20 min) |
| `task_time_limit` | 1300 s (hard kill) |
| `flow.exfiltrate_chat` soft limit | 2400 s |
| `flow.exfiltrate_chat` hard limit | 2500 s |

### 6.6 Self-Healing

- **`system.self_heal` (every 6 h):** iterates all `active` credentials; recreates missing forum topics; triggers catch-up broadcast
- **`audit.audit_active_topics` (hourly):** sends silent `send_chat_action` probe per topic; detects deleted topics; clears stale `topic_id` from meta; triggers re-enrichment
- **Inline token self-heal:** any non-Fernet token encountered during enrichment or exfiltration is encrypted in place before processing

---

## 7. Observability

### 7.1 Logging

**Format (all services):**
```
2026-04-21 14:23:45 | INFO | scanner.tasks | [Shodan] Processing 250 matches...
```

Logging force-initialised via `logging.basicConfig(..., force=True)` in both `app/api/main.py` and `app/workers/celery_app.py`. `uvicorn.access` logger set to WARNING to reduce noise.

### 7.2 Metrics (`app/core/metrics.py`)

`MetricsCollector` singleton (`metrics`):
- `metrics.track("name")`  decorator; records success/failure counts and duration (min/max/avg)
- `metrics.inc("counter")`  increment a named counter
- `metrics.get_all_metrics()`  full dict of all tracked operations
- `metrics.get_summary()`  aggregate success rate

Exposed at `GET /health/metrics`.

### 7.3 Audit Logging (`app/core/audit.py`)

`AuditLogger.log()` records security events to application logs. High-importance events (`TOKEN_DECRYPTED`, `TOKEN_REVOKED`, `CREDENTIAL_CREATED`) are also persisted to the `audit_logs` database table via `_persist_to_db()`.

### 7.4 Watchdog (Bot Listener)

`watchdog_loop()` runs in the bot service process. Checks Redis connectivity every 60 s. Alerts the monitor group if Redis is unreachable or if the worker heartbeat (`system:heartbeat:last_seen` Redis key) is older than 45 minutes.

---

## 8. Known Limitations

1. **No migration framework**  schema changes require manual DDL re-application via `database/init.sql` (idempotent).
2. **Single Redis instance**  no HA or persistence tuning beyond Docker volume.
3. **No built-in TLS**  requires external reverse proxy (nginx, Caddy).
4. **No rate limiting on API endpoints** beyond optional `MONITOR_API_KEY` header.
5. **No alerting framework**  observability is log/metric based; external aggregation required.
6. **No Kubernetes support**  Docker Compose only.
7. **Horizontal scaling**  broadcast exactly-once guarantee holds only with shared Supabase DB; at-least-once on multi-machine deployments.
8. **`CENSYS_ID`/`CENSYS_SECRET` and `HYBRID_ANALYSIS_KEY`** are present in `Settings` but have no corresponding scanner implementation. These keys are accepted but unused.
