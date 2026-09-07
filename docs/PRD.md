# Product Requirements Document — theprawnhunter

**Source commit:** `remediation/2026-09-06` branch  
**Documentation date:** 2026-09-07  
**Principle:** Every claim in this document is derived from source code, live container inspection, or Supabase schema query. Unverified items are prefixed `[unverified]`.

---

## 1. Executive Summary

This system is a self-hosted, continuously-running OSINT pipeline that discovers exposed Telegram Bot API tokens across public data sources, validates each token against the live Telegram API, harvests accessible chat history from every confirmed bot, and delivers the findings to a private Telegram supergroup organised as per-bot forum topics.

The deployment runs as a Docker Compose stack of 10 services on a single host. All external traffic reaches the host through Cloudflare Tunnel — no ports are directly exposed. A managed PostgreSQL instance (Supabase) persists all state. A read-only Next.js dashboard provides a browser view of the findings queue and chat evidence. A Manifest V3 Chrome extension scrapes FOFA search pages and feeds raw tokens into the ingestion pipeline.

---

## 2. System Architecture

### 2.1 Service topology

| Service | Image | Role | Queues consumed |
|---|---|---|---|
| `redis` | redis:7-alpine | Celery broker + result backend + rate-limit + locks | — |
| `api` | built (gunicorn -w 4) | FastAPI HTTP service | — |
| `worker-core` | built | General task execution | `celery` |
| `worker-scanners` | built | OSINT scanner tasks + GitHub Events firehose | `scanners` |
| `worker-scrape` | built | Telethon history scraping + rescrape | `scrape` |
| `worker-validators` | built | Token validation + pivot fan-out | `validation` |
| `beat` | built | Celery periodic task scheduler (60 beat entries) | — |
| `bot` | built | `python-telegram-bot` admin command listener | — |
| `flower` | built | Celery task monitor UI | — |
| `frontend` | Next.js standalone | Read-only analyst dashboard | — |

### 2.2 Data flow

```
[OSINT Sources × 21 scanner classes]
         │  regex extraction + format validation
         ▼
[Token Queue — validation/ Celery queue]
         │  global Redis token-bucket rate limiter (30 calls/10 s)
         │  Telegram getMe + getWebhookInfo
         ▼
[discovered_credentials — Supabase]
         │  Fernet-encrypted OR plaintext (PLAINTEXT_TOKEN_MODE=True)
         ▼
[flow.enrich_credential — celery/ queue]
         │  Telethon get_dialogs, confidence scoring, forum topic creation
         ▼
[flow.exfiltrate_chat — scrape/ queue]
         │  4-strategy scraper (Bot API, Telethon, ID bruteforce, forwarding)
         ▼
[exfiltrated_messages — Supabase]
         │  upsert on (credential_id, telegram_msg_id)
         ▼
[flow.broadcast_pending — celery/ queue, every 1 min]
         │  DB-level atomic claim (broadcast_claimed_at)
         │  python-telegram-bot sendMessage/sendPhoto/sendDocument
         ▼
[Monitor Supergroup — per-bot forum topics]

[honeypot_updates — Supabase]  ← POST /honeypot/receive/{id}
         │  flow.honeypot_redirect_sweep (every 30 s)
         ▼
[Captured bot sends redirect to victim user]

[Chrome Extension]
         │  POST /ingest/extension/credentials OR direct Supabase REST
         ▼
[discovered_credentials — same pipeline]
```

### 2.3 State storage

| Store | Technology | What lives there |
|---|---|---|
| Supabase PostgreSQL | Managed Postgres 17.6 | All persistent state (23 tables, 3 views) |
| Redis 7 | Docker volume `telegramhunter_redis_data` | Broker, rate-limit buckets, dedup keys, session leases, heartbeat |
| Filesystem volumes | `telegramhunter_sessions`, `telegramhunter_imports`, `telegramhunter_beat_schedule` | Telethon session files, CSV drop-in, beat schedule state |

---

## 3. Feature Matrix

| Feature | Module / Path | Status | Notes |
|---|---|---|---|
| Multi-source token discovery | `app/services/scanners.py`, `app/services/scanners_extension.py` | **Implemented** | 21 scanner classes; see §3 scanner list |
| Token validation (Telegram `getMe`) | `app/workers/tasks/validation_tasks.py` | **Implemented** | Redis token-bucket rate limiting; dedup via `validated:recent:<sha256>` |
| Pivot fan-out (GitHub owner, bot username, webhook host) | `app/workers/tasks/pivot_tasks.py` | **Implemented** | Fires after every successful `getMe` |
| GitHub Events real-time firehose | `app/workers/tasks/firehose_tasks.py` | **Implemented** | ETag-aware 30 s polling; `firehose.poll_github_events` |
| Credential enrichment + confidence scoring | `app/workers/tasks/flow_tasks.py:enrich_credential` | **Implemented** | `collection_yield_score` + `chat_member_count` as generated columns |
| 4-strategy chat scraping | `app/services/scraper_srv.py`, `app/services/_scraper/` | **Implemented** | Bot API → Telethon → ID bruteforce → forwarding archive |
| Broadcast to monitor supergroup | `app/workers/tasks/flow_tasks.py:broadcast_pending`, `app/services/broadcaster_srv.py` | **Implemented** | Atomic DB claim; parallel-by-cred_id with Semaphore; `broadcast_message_id` idempotency |
| Permanent broadcast failure cap | `flow_tasks._mark_broadcast_failure` | **Implemented** | `MAX_BROADCAST_ATTEMPTS=8`; `broadcast_status='permanent_failed'` |
| Webhook fingerprinting + takeover | `flow_tasks.probe_webhooks`, `flow_tasks.force_webhook_takeover_pass` | **Implemented** | Detects third-party C2 webhooks; optional `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE` |
| Honeypot push receiver | `app/api/routers/honeypot.py`, `flow_tasks.honeypot_redirect_sweep` | **Implemented** | Active only when `HONEYPOT_MODE=True` + public HTTPS endpoint |
| Honeypot redirect injection | `flow_tasks.honeypot_redirect_one`, `honeypot_redirect_tasks.py` | **Implemented** | Requires both `HONEYPOT_REDIRECT_MODE=True` AND `HONEYPOT_REDIRECT_AUTHORIZED=True` |
| Multi-touch redirect follow-ups | `app/workers/tasks/honeypot_redirect_tasks.py` | **Implemented** | Touch 2 (daily), Touch 3 (daily), proactive outreach (6 h) |
| Perceptual-hash media forensics | `flow_tasks.hash_exfil_media` | **Implemented** | SHA-256 + `imagehash.phash`; cross-bot duplicate detection |
| Telemetry indicator extraction | `app/services/telemetry_parser.py`, `flow_tasks._index_telemetry_indicators` | **Implemented** | Wallet addresses, network domains, phone numbers |
| Insight / findings queue | `app/services/findings.py`, `flow_tasks.produce_findings`, `flow_tasks.route_finding_deltas` | **Implemented** | Priority-first analyst queue; `findings` + `finding_evidence` tables |
| Finding alert policies | `app/services/finding_alerts.py` | **Implemented** | Policy-gated; `FINDING_ALERTS_ENABLED=False` default |
| Entity graph | `app/services/entities.py`, `flow_tasks.build_entity_graph` | **Implemented** | `entities` + `entity_edges` tables |
| Engagement funnel tracking | `app/services/engagement.py` | **Implemented** | HMAC-pseudonymised subject IDs; `engagement_events` table |
| C2 operator clustering | `flow_tasks.cluster_c2_operators` | **Implemented** | Groups bots by shared webhook host / Shodan org |
| Attribution graph | `flow_tasks.attribution_graph_report` | **Implemented** | Weekly; links user_ids across multiple captured bots |
| CSV token import | `app/workers/tasks/import_tasks.py` | **Implemented** | Drop CSV in `imports/`; atomic `.pending` claim + `.done` breadcrumb |
| Admin bot commands | `app/services/bot_listener.py` | **Implemented** | `/status`, `/pause`, `/resume`, `/restart`, `/bots`, `/starthunter`, `/telemetry`, `/getfile`, and more |
| Telethon account login (`/starthunter`) | `bot_listener.py:ConversationHandler` | **Implemented** | Interactive 3-step (phone → code → password); 180 s timeout; orphan session sweep |
| FastAPI monitor API (32 endpoints) | `app/api/routers/` | **Implemented** | Monitor-key gated; Redis-backed stats cache; `/monitor/search`, `/monitor/findings`, `/monitor/operators` |
| Canary flow check | `flow_tasks.canary_flow_check`, `flow_tasks.canary_findings_check` | **Implemented** | Two separate canaries: raw broadcast path + findings pipeline |
| System heartbeat | `flow_tasks.system_heartbeat` | **Implemented** | Metrics flush + Redis timestamp; every 30 min |
| Audit logging | `app/core/audit.py` | **Implemented** | Token-redacting; 8 KB payload cap; 7-day retention |
| RLS + redacted evidence view | `database/rls_policies.sql` | **Implemented** | `evidence_redacted` view for authenticated operators; `discovered_credentials_public` view |
| Rate limiting (API) | `app/api/main.py` (slowapi) | **Implemented** | 120 req/min per key/IP; Redis-backed; constant-time key compare |
| Circuit breakers | `app/core/circuit_breaker.py` | **Implemented** | Per-scanner; threshold=3, recovery=300 s default |
| Queue depth monitoring | `app/core/queue_monitor.py`, `/health/queues` | **Implemented** | Oldest-job-age tracking per queue |
| Next.js analyst dashboard | `frontend/` | **Implemented** | Supabase anon/authenticated; `findings` view + `ChatWindow` + `TelemetryAnalyticsView` |
| Chrome extension (FOFA scraper) | `extension/` | **Implemented** | Manifest V3; 49-country scan; uploads to `/ingest/extension/credentials` |
| SerperService scanner | — | **Deprecated** | Class removed; `SERPER_API_KEY` silently ignored |

**Active scanner classes (21):**
Shodan, FOFA, URLScan, GitHub, GitLab, Exa, Wayback Machine, Common Crawl, Sourcegraph, GitHub Gist, grep.app, PublicWWW, Google Custom Search, Bitbucket, Pastebin, Rentry, Hastebin, Netlas, Replit, Postman, Searchcode.

Beat schedule: **60 entries** covering broadcast, rescrape, scanners, audit, honeypot redirects, media hashing, entity graph, canary, heartbeat, and system maintenance.

---

## 4. Data Model

### 4.1 Live Supabase tables (23)

| Table | Purpose |
|---|---|
| `discovered_credentials` | Validated bot tokens; `collection_yield_score` + `chat_member_count` as STORED generated columns |
| `exfiltrated_messages` | Chat history; `broadcast_status`, `broadcast_message_id`, `next_retry_at` for retry logic |
| `monitor_stats` | Singleton aggregate counters; maintained by triggers |
| `telegram_accounts` | Telethon session accounts added via `/starthunter` |
| `audit_logs` | Security audit events; 7-day retention; 8 KB payload cap |
| `telemetry_indicators` | Structured extractions (wallet addresses, domains, phones) |
| `media_hashes` | SHA-256 + perceptual hashes; `is_failure BOOLEAN`, `failure_reason TEXT`, `UNIQUE(message_id)` |
| `honeypot_updates` | Incoming webhook push payloads from taken-over bots |
| `findings` | Priority-first analyst insight queue |
| `finding_evidence` | Provenance records for findings |
| `finding_feedback` | Analyst dispositions on findings |
| `finding_summaries` | Aggregated finding summary rows |
| `finding_alert_policies` | Rules governing when alerts are routed outbound |
| `finding_alert_audit` | Delivery audit trail |
| `finding_alert_deliveries` | Outbound alert delivery records |
| `entities` | Named entities extracted from messages |
| `entity_edges` | Relationships between entities |
| `engagement_events` | Pseudonymised funnel events (HMAC subject IDs) |
| `system_state` | Key-value store for worker coordination |
| `keepalive_log` | Daily GitHub Actions keepalive pings |
| `retention_archive` | Archived rows from retention cleanup |
| `retention_cleanup_runs` | Audit log of retention operations |
| `keepalive_logs` | Legacy keepalive table (pre-2026-09 naming) |

### 4.2 Views

| View | Access | Purpose |
|---|---|---|
| `discovered_credentials_public` | `authenticated` only | Safe projection — no `bot_token`, `token_hash`, `chat_id` |
| `evidence_redacted` | `authenticated` only | Content token-masked, sender HMAC-pseudonymised, truncated to 500 chars |
| `engagement_funnel_daily` | `authenticated` | Daily funnel aggregation |

### 4.3 Key constraints

- `discovered_credentials.token_hash` — `UNIQUE`
- `exfiltrated_messages.(credential_id, telegram_msg_id)` — `UNIQUE`
- `media_hashes.message_id` — `UNIQUE WHERE is_failure = FALSE`
- `monitor_stats.id` — `UNIQUE` (singleton boolean PK)

### 4.4 Token storage

`PLAINTEXT_TOKEN_MODE=True` is active in the current deployment. `bot_token` columns contain plaintext Telegram token strings. `security.encrypt()` is a no-op when this flag is set; `security.decrypt()` handles both ciphertext (`gAAAA%` prefix) and plaintext transparently for backward compatibility.

### 4.5 Migrations

25 migrations in `supabase/migrations/`, applied to the live database. All are idempotent (`IF NOT EXISTS` guards). Legacy pre-supabase-CLI patches in `docs/history/legacy_migrations/` (8 files) — historical reference only.

---

## 5. External Interfaces

### 5.1 HTTP API (FastAPI, port 8011 on host, bound to 127.0.0.1)

All non-health endpoints require `X-Monitor-Key` header (constant-time compare). Rate limit: 120 req/min per key/IP (Redis-backed, cross-worker).

**Health**

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | None | Liveness; `{"status":"active"}` in production |
| GET | `/health/` | None | Always 200 |
| GET | `/health/detailed` | Key | DB + Redis + Bot API connectivity |
| GET | `/health/metrics` | Key | In-memory metric counters |
| GET | `/health/queues` | Key | Queue depths + oldest-job age |
| GET | `/health/operational` | Key | Canary + broadcast + scrape failure summary |
| GET | `/health/circuit-breakers` | Key | Per-scanner circuit breaker state |
| POST | `/health/circuit-breakers/{name}/reset` | Key | Force-reset a named breaker |
| GET | `/health/quotas` | Key | Redis memory + queue stats |
| GET | `/health/bot-pool` | Key | Bot pool rotation state |

**Monitor (analytics / operational)**

| Method | Path | Description |
|---|---|---|
| GET | `/monitor/stats` | Aggregate counts (30 s Redis cache) |
| GET | `/monitor/credentials` | Credential list; sortable by `collection_yield_score`, `chat_member_count`, etc. |
| GET | `/monitor/messages` | Recent exfiltrated messages |
| GET | `/monitor/findings` | Priority-first findings queue |
| GET | `/monitor/findings/{id}` | Finding detail + bounded evidence |
| GET | `/monitor/findings/{id}/evidence` | Paginated evidence |
| POST | `/monitor/findings/{id}/feedback` | Analyst disposition (RPC `record_finding_feedback_service`) |
| POST | `/monitor/engagement/lifecycle` | HMAC-pseudonymised funnel event |
| GET | `/monitor/export` | CSV / JSON export of messages |
| GET | `/monitor/broadcasts/pending` | Pending + retry metadata |
| POST | `/monitor/broadcasts/{id}/retry` | Manual retry trigger |
| POST | `/monitor/topics/revoked/close` | Close topics for revoked credentials |
| GET | `/monitor/webhooks` | Captured C2 webhook URLs |
| GET | `/monitor/targets/export` | Target feed export |
| GET | `/monitor/search` | Full-text search across messages (`pg_trgm`) |
| GET | `/monitor/operators` | C2 operator cluster report |

**Ingest**

| Method | Path | Description |
|---|---|---|
| POST | `/ingest/extension/credentials` | Bulk token ingest; server-side encryption |
| POST | `/ingest/tokens` | Plain-text / JSON-array token paste |

**Scan** (development only — 403 in `ENV=production`)

| Method | Path | Description |
|---|---|---|
| POST | `/scan/trigger` | Enqueue named scanner task |

**Media**

| Method | Path | Description |
|---|---|---|
| GET | `/media/{message_id}` | Proxy media from Telegram via source bot token |

**Honeypot**

| Method | Path | Description |
|---|---|---|
| POST | `/honeypot/receive/{credential_id}` | Telegram webhook push receiver (active only when `HONEYPOT_MODE=True`) |
| GET | `/honeypot/status` | Configuration state; returns `mode_enabled`, `receiver_url_configured`, `allowlist_configured` |

### 5.2 Telegram admin commands

Accepted from whitelisted admins or `ANONYMOUS_ADMIN_ID` in the monitor supergroup, or via DM:

`/start`, `/stop`, `/optout`, `/unsubscribe`, `/status`, `/pause`, `/resume`, `/restart`, `/help`, `/commands`, `/bots`, `/telemetry`, `/indicators`, `/getfile`, `/archive`, `/backfill`, `/starthunter` (ConversationHandler), `/cancel`

### 5.3 External integrations

| Service | How used | Auth |
|---|---|---|
| Telegram Bot API | Validation (`getMe`, `getWebhookInfo`), broadcasting, honeypot | Bot token in URL |
| Telegram MTProto (Telethon) | Chat history scraping, user-agent account sessions | Session file + `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` |
| Supabase | Persistent storage | `SUPABASE_SERVICE_ROLE_KEY` (backend); `SUPABASE_KEY` anon (frontend/extension) |
| Redis | Broker, rate limits, locks, dedup cache | `REDIS_URL` |
| 21 OSINT scanner APIs | Token discovery | Per-service API keys (all optional) |
| Cloudflare Tunnel (optional) | Public HTTPS endpoint for honeypot | `cloudflared` service |

---

## 6. Security Posture

**Authentication / Authorization:**
- All monitor API endpoints: `X-Monitor-Key` header, constant-time `hmac.compare_digest` compare (`app/core/auth.py`)
- `/scan/trigger` blocked in `ENV=production`
- `/docs`, `/redoc`, `/openapi.json` blocked in `ENV=production`
- Admin bot commands: numeric user-ID whitelist only (username-based auth explicitly rejected as insecure)
- Honeypot receiver: `X-Telegram-Bot-Api-Secret-Token` header validation + per-credential allowlist

**Supabase RLS:**
- Service-role key bypasses all RLS (backend only — never exposed to browser)
- Anon key: `discovered_credentials` raw table — fully denied; `discovered_credentials_public` view — denied (authenticated only); `exfiltrated_messages` — denied; `evidence_redacted` view — authenticated only
- Extension direct-write: `x-extension-secret` header matched against `app.extension_write_secret` DB parameter (never in code or env files)

**Token storage:**
- `PLAINTEXT_TOKEN_MODE=True` is active — tokens are stored as plaintext in `discovered_credentials.bot_token`. Fernet encryption infrastructure (`SecurityService`, `ENCRYPTION_KEY`, `ENCRYPTION_KEY_LEGACY`) remains in place; toggling `PLAINTEXT_TOKEN_MODE=False` re-enables encryption for new writes.
- `AuditLogger` redacts token-shaped strings from all log output and audit event `details` payloads before persistence.

**Transport:**
- All Docker service ports bound to `127.0.0.1` — no direct external exposure
- External reach via Cloudflare Tunnel (optional) or reverse proxy — not TLS-terminated by the stack itself

**Rate limiting:**
- `slowapi` 120 req/min per key/IP on all API endpoints; Redis-backed cross-worker
- Telegram `getMe` rate limiter: 30 calls/10 s global Redis token bucket across all validator workers

**Known absent:**
- No IP allowlisting on the monitor API (key-only auth)
- No 2FA on the Flower dashboard beyond `FLOWER_BASIC_AUTH`
- `FINDING_ALERTS_ENABLED=False` — outbound alert delivery disabled by default

---

## 7. Performance & Scalability

**Broadcast throughput:**
- Current: 1 monitor bot, `BROADCAST_MAX_PARALLEL_TOPICS=1`, `BROADCAST_INTER_MESSAGE_DELAY_SECONDS=5.0`, `BROADCAST_BATCH_SIZE=50`
- Observed: ~10 messages/min sustained without Telegram flood_wait. Scales linearly with number of monitor bots added to `MONITOR_BOT_TOKEN`.
- Parallelism model: messages grouped by `credential_id`; up to `BROADCAST_MAX_PARALLEL_TOPICS` groups run concurrently via `asyncio.gather` + Semaphore. Within each group, messages are sequential.

**Token validation:**
- Global Redis token bucket: 30 `getMe` calls/10 s across all 16 validator workers.
- Cross-source dedup: 24 h Redis key per token (`validated:recent:<sha256>`) eliminates redundant API calls.

**Scraping:**
- Per-credential Telethon timeout: `TELEGRAM_HISTORY_TIMEOUT_SECONDS=90`
- Scrape queue backpressure: `RESCRAPE_BACKPRESSURE_THRESHOLD=100`

**Database (current live state):**
- Supabase free tier (500 MB); current DB size: ~144 MB (28.9%) after VACUUM FULL following bulk deletion of 290k broadcast-delivered messages older than 30 days.
- `exfiltrated_messages`: 67,077 rows (post-prune)
- `audit_logs`: ~150k rows (7-day retention)
- Composite index on `audit_logs(event_type, timestamp DESC)` for `/health/operational` queries

**Persistent event loop:**
- One `asyncio` event loop per Celery worker process (`get_worker_loop()` in `celery_app.py`). All async tasks share the loop via `loop.run_until_complete()`. This preserves `asyncio.Lock` semantics across tasks and enables Telethon connection reuse within a process.

---

## 8. Non-Functional Behavior

**Error handling:**
- All FastAPI exception handlers return generic `{"detail": "Internal error"}` — no stack traces in responses
- `AuditLogger._persist_to_db` failures are caught and logged, never raise (audit must not break the main flow)
- Broadcast exceptions: `BroadcastSendError(reason, detail, retryable, retry_after_seconds)` — retryable/permanent classification gates retry vs permanent_failed transition

**Logging:**
- stdlib `logging` with `%(asctime)s | %(levelname)s | %(name)s | %(message)s` format; container stdout
- Docker json-file driver: 10 MB max / 3 rotations per service
- `broad-except` sites emit `logger.debug(f"[suppressed] {exc}")` (68 sites patched in 2026-09 remediation cycle)

**Retry / timeout:**
- External HTTP calls: `httpx.AsyncClient(timeout=10.0–30.0)` per site; `retry_with_backoff` in `app/utils/http_client.py` (exponential, handles 429 / 5xx / network errors)
- Celery task soft limit: 1200 s; hard limit: 1800 s
- Exfiltrate soft limit: 2400 s, hard: 2500 s
- Broadcast lock renews every 90 s via background thread while batch runs

**Health checks:**
- `api`: Python `urllib.request.urlopen('http://localhost:8001/health/')`, interval 30 s, timeout 45 s
- `bot`: file `/tmp/bot_alive` touched every 10 s; check: file modified within 60 s
- All workers: `python3 -c 'import redis; r=redis.from_url(...); r.ping()'`, interval 60 s, timeout 180 s
- `flower`: TCP connect port 5555, interval 30 s, timeout 5 s
- `frontend`: `node -e "require('http').get('http://localhost:3000/',…)"`, interval 30 s, timeout 15 s

**Graceful shutdown:**
- `worker_shutdown` Celery signal closes the persistent event loop and sends a Telegram notification
- FastAPI lifespan sends shutdown notification on a daemon thread (non-blocking)
- Bot listener: SIGINT/SIGTERM sets stop_event; poll loops exit cleanly

**Canary:**
- `flow.canary_flow_check`: synthetic DB → broadcast → optional frontend ping (requires `CANARY_CREDENTIAL_ID`)
- `flow.canary_findings_check`: synthetic findings insert → visibility verify → cleanup (no config required; fails gracefully if `findings` table absent)

---

## 9. Known Limitations

1. **Single monitor bot**: current deployment has 1 `MONITOR_BOT_TOKEN`. Broadcast throughput (~10 msg/min) is limited by Telegram flood control. Adding more bots to `MONITOR_BOT_TOKEN` scales linearly.

2. **No DATABASE_URL in environment**: DDL migrations can only be applied via the Supabase Management API or SQL editor, not via `psql` or `supabase db push` locally. Add `DATABASE_URL` (Postgres connection string from Supabase Project Settings) to unlock direct migration tooling.

3. **Broad-except sites**: 68 `except Exception:` sites were patched to emit `logger.debug` in the 2026-09 cycle. Many are intentional best-effort paths; a full per-site review is deferred to the next cycle.

4. **`PLAINTEXT_TOKEN_MODE=True`**: tokens at rest are plaintext in the current deployment. The Fernet encryption path is preserved and toggleable but inactive. Any direct database access exposes token values.

5. **5 pre-existing test failures in `test_honeypot_redirect_bugs.py`**: the async tests that verify callback and inline query paths need `HONEYPOT_REDIRECT_AUTHORIZED=True` patches — not added in the current test fix cycle (separate NEW-002 issue).

6. **Supabase free tier**: DB capped at 500 MB. Current usage is ~144 MB (healthy) but grows at ~50–100 MB/month depending on scrape volume. Bulk retention pruning (quarterly) or upgrade to Supabase Pro required for sustained operation.

7. **Flower dashboard**: no healthcheck on the Docker service as of restart — the TCP-connect healthcheck was added but the live container needs a restart to apply it. Container is healthy in practice; the check resolves on next restart.

8. **`keepalive_logs` vs `keepalive_log`**: two tables exist in the live schema — the plural `keepalive_logs` is a legacy artifact; canonical usage is `keepalive_log` (singular).

9. **CI red on `main`**: the `quality` CI job fails on 161 ruff issues in `app/` (down from 192 baseline; delta -31 from this cycle). Dedicated `chore(quality)` cycle needed to zero remaining issues.

10. **Token regex in `scripts/telegram_behavior_probe.py`**: not verified against the canonical `\d{8,15}:[A-Za-z0-9_-]{35}` pattern used in `app/services/scanners.py`. Probe file is test-only and not in the hot path.
