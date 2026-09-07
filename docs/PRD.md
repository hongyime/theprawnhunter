# Product Requirements Document — The Prawn Hunter

**Source commit:** `39cb279` (branch `main`)
**Documentation date:** 2026-09-07
**Principle:** Every claim is derived from source code, live container inspection, or Supabase schema query. Unverified items are prefixed `[unverified]`.

---

## 1. Executive Summary

The Prawn Hunter is a self-hosted OSINT pipeline that discovers exposed Telegram Bot API tokens across 21 public data sources, validates each token against the live Telegram API, harvests accessible chat history from every confirmed bot, and delivers findings to a private Telegram supergroup organised as per-bot forum topics. It also exposes a browser-based analyst dashboard at `theprawnhunter.vercel.app` for querying the findings queue, browsing captured evidence, and triaging intelligence. The system runs as a Docker Compose stack of 10 services on a single host, backed by Supabase managed PostgreSQL and Redis.

---

## 2. System Architecture

### 2.1 Service topology (10 services)

| Service | Image | Role | Celery queues |
|---|---|---|---|
| `redis` | redis:7-alpine | Broker + result backend + rate limits + locks | — |
| `api` | built (gunicorn -w 4) | FastAPI HTTP service, 31 endpoints | — |
| `worker-core` | built | General orchestration tasks | `celery` |
| `worker-scanners` | built | OSINT scanner + GitHub Events firehose | `scanners` |
| `worker-scrape` | built | Telethon scraping + rescrape | `scrape` |
| `worker-validators` | built | Token validation + pivot fan-out | `validation` |
| `beat` | built | Periodic task scheduler (60 schedule entries) | — |
| `bot` | built | Telegram admin command listener | — |
| `flower` | built | Celery task monitor UI | — |
| `frontend` | Next.js 16 standalone | Analyst dashboard (`theprawnhunter.vercel.app`) | — |

### 2.2 Data flow

```
[21 Scanner Sources]
        │ regex + format validation
        ▼
[validation/ queue]
        │ Redis token-bucket rate limiter (30 getMe calls / 10 s global)
        │ Telegram getMe + getWebhookInfo
        ▼
[discovered_credentials — Supabase]
        │ plaintext bot_token (PLAINTEXT_TOKEN_MODE=True)
        ▼
[flow.enrich_credential — celery/ queue]
        │ Telethon get_dialogs → confidence scoring → forum topic creation
        ▼
[flow.exfiltrate_chat — scrape/ queue]
        │ 4-strategy scraper
        ▼
[exfiltrated_messages — Supabase]
        │ upsert (credential_id, telegram_msg_id)
        ▼
[flow.broadcast_pending — every 1 min]
        │ DB-level atomic claim → adaptive-delay send → mark sent
        ▼
[Monitor Supergroup — per-bot forum topics]

[honeypot_updates] ← POST /honeypot/receive/{id}
        │ flow.honeypot_redirect_sweep (30 s)
        ▼
[Captured bot → sendMessage → victim user]
```

### 2.3 State storage

| Store | Technology | Contents |
|---|---|---|
| Supabase PostgreSQL | Managed Postgres 17.6 | 23 tables, 3 views, all persistent state |
| Redis 7 | Volume `telegramhunter_redis_data` | Celery broker/backend, rate buckets, dedup keys, adaptive broadcast delay, session leases |
| Filesystem | Volumes `telegramhunter_sessions`, `_imports`, `_beat_schedule` | Telethon `.session` files, CSV drop-ins, beat schedule state |

### 2.4 Authentication (multi-layer)

- **API**: `X-Monitor-Key` header, constant-time `hmac.compare_digest` (`app/core/auth.py:require_monitor_key`)
- **Dashboard**: GitHub OAuth 2.0 via Supabase Auth; requires `app_metadata.operator=true` JWT claim
- **Honeypot receiver**: `X-Telegram-Bot-Api-Secret-Token` header
- **Admin bot**: numeric Telegram user-ID whitelist only

---

## 3. Feature Matrix

| Feature | Path | Status | Notes |
|---|---|---|---|
| Multi-source token discovery | `app/services/scanners.py`, `scanners_extension.py` | Implemented | 21 scanner classes |
| Token validation (`getMe`) | `app/workers/tasks/validation_tasks.py` | Implemented | Global Redis token-bucket; 24h dedup cache |
| Pivot fan-out | `app/workers/tasks/pivot_tasks.py` | Implemented | GitHub owner, bot username, webhook host |
| GitHub Events firehose | `app/workers/tasks/firehose_tasks.py` | Implemented | ETag-aware, 30 s cadence |
| Credential enrichment + confidence scoring | `flow_tasks.py:enrich_credential` | Implemented | `collection_yield_score` + `chat_member_count` as STORED generated columns |
| 4-strategy chat scraping | `app/services/scraper_srv.py`, `_scraper/` | Implemented | Bot API → Telethon → ID bruteforce → forwarding archive |
| Parallel broadcast | `flow_tasks.py:_broadcast_logic` | Implemented | Groups by `cred_id`; `asyncio.gather` + Semaphore; adaptive delay (1–10 s, Redis-persisted); `broadcast_message_id` idempotency |
| Permanent broadcast failure cap | `flow_tasks.py:_mark_broadcast_failure` | Implemented | `MAX_BROADCAST_ATTEMPTS=8`; `broadcast_status='permanent_failed'` |
| Webhook fingerprinting + takeover | `flow_tasks.probe_webhooks`, `force_webhook_takeover_pass` | Implemented | Optional TLS verify via `TLS_VERIFY_WEBHOOK_PROBES` |
| Honeypot push receiver | `app/api/routers/honeypot.py` | Implemented | Active when `HONEYPOT_MODE=True` |
| Honeypot redirect injection | `flow_tasks.honeypot_redirect_one`, `honeypot_redirect_tasks.py` | Implemented | Requires both `HONEYPOT_REDIRECT_MODE=True` + `HONEYPOT_REDIRECT_AUTHORIZED=True` |
| Multi-touch redirect follow-ups | `app/workers/tasks/honeypot_redirect_tasks.py` | Implemented | Touch 2 (daily), Touch 3 (daily), proactive outreach (6 h) |
| Perceptual-hash media forensics | `flow_tasks.hash_exfil_media` | Implemented | SHA-256 + `imagehash.phash`; `UNIQUE(message_id)` on `media_hashes` |
| Telemetry indicator extraction | `app/services/telemetry_parser.py` | Implemented | Wallet addresses, domains, phones |
| Findings / analyst insight queue | `app/services/findings.py`, `flow_tasks.produce_findings` | Implemented | `findings`, `finding_evidence` tables; priority-first |
| Finding alert policies | `app/services/finding_alerts.py` | Implemented | Policy-gated; `FINDING_ALERTS_ENABLED=False` default |
| Entity graph | `app/services/entities.py`, `flow_tasks.build_entity_graph` | Implemented | `entities`, `entity_edges`, `engagement_events` tables |
| C2 operator clustering | `flow_tasks.cluster_c2_operators` | Implemented | Groups bots by shared webhook host / Shodan org |
| CSV token import | `app/workers/tasks/import_tasks.py` | Implemented | `.done` breadcrumb; atomic `.pending` claim |
| Admin bot commands | `app/services/bot_listener.py` | Implemented | `/status`, `/pause`, `/resume`, `/restart`, `/bots`, `/starthunter`, `/telemetry`, `/getfile`, `/backfill` |
| Telethon account login (`/starthunter`) | `bot_listener.py:ConversationHandler` | Implemented | 3-step interactive; 180 s timeout; orphan session sweep |
| FastAPI monitor API | `app/api/routers/` | Implemented | 31 endpoints; Redis-backed stats cache; `/monitor/findings`, `/monitor/search`, `/monitor/operators` |
| Findings canary | `flow_tasks.canary_findings_check` | Implemented | Synthetic findings insert → verify → cleanup; hourly |
| Broadcast throughput metric | `flow_tasks._broadcast_logic`, Redis key `metrics:broadcast:throughput_per_min` | Implemented | Persisted per run, 3600 s TTL |
| GitHub SSO | Supabase Auth + `frontend/app/signin/page.tsx` | Implemented | OAuth 2.0; operator claim gates dashboard access; signup locked |
| Next.js analyst dashboard | `frontend/` | Implemented | `theprawnhunter.vercel.app`; Findings, Chat, Telemetry views |
| Chrome extension (FOFA scraper) | `extension/` | Implemented | Manifest V3; 49-country scan; `/ingest/extension/credentials` |
| RLS + redacted evidence view | `database/rls_policies.sql` | Implemented | `evidence_redacted` view; `discovered_credentials_public` view; all authenticated-only |
| MTProto client pooling | `app/services/bot_manager_srv.py` | Implemented | `dict[token → TelegramClient]`, max 50, disconnect on shutdown |

---

## 4. Data Model

### 4.1 Live Supabase tables (23)

| Table | Key columns / constraints |
|---|---|
| `discovered_credentials` | `token_hash UNIQUE`; `bot_token` plaintext; `collection_yield_score` + `chat_member_count` STORED generated from `meta` JSONB |
| `exfiltrated_messages` | `(credential_id, telegram_msg_id) UNIQUE`; `broadcast_status TEXT CHECK('pending','sent','permanent_failed','revoked')`; `broadcast_message_id BIGINT` |
| `monitor_stats` | Singleton row; maintained by triggers `trg_monitor_stats_*` |
| `telegram_accounts` | Session accounts; `locked_by`, `locked_until` (10-min lease) |
| `audit_logs` | 7-day retention; 8 KB payload cap; composite index `(event_type, timestamp DESC)` |
| `telemetry_indicators` | `(message_id, indicator_type, indicator_value) UNIQUE` |
| `media_hashes` | `UNIQUE(message_id) WHERE is_failure=FALSE`; `is_failure BOOLEAN`, `failure_reason TEXT` |
| `honeypot_updates` | `redirected_at` used as `'pending'` sentinel during claim-before-dispatch |
| `findings` | Priority-first analyst queue |
| `finding_evidence` | Provenance for findings |
| `finding_feedback` | Analyst dispositions |
| `finding_summaries` | Aggregated finding rows |
| `finding_alert_policies` | Outbound alert routing rules |
| `finding_alert_audit` | Delivery audit trail |
| `finding_alert_deliveries` | Outbound alert records |
| `entities` | Named entities from messages |
| `entity_edges` | Entity relationships |
| `engagement_events` | HMAC-pseudonymised funnel events |
| `system_state` | Worker coordination key-value |
| `keepalive_log` | Daily GitHub Actions keepalive pings (singular — see Known Limitations) |
| `retention_archive` | Archived rows from retention cleanup |
| `retention_cleanup_runs` | Retention operation audit |
| `keepalive_logs` | Legacy plural table (artifact) |

### 4.2 Views

| View | Access | Purpose |
|---|---|---|
| `discovered_credentials_public` | `authenticated` only | No `bot_token`, `token_hash`, `chat_id` |
| `evidence_redacted` | `authenticated` only | Content token-masked, sender HMAC-pseudonymised, 500-char truncation |
| `engagement_funnel_daily` | `authenticated` | Daily funnel aggregation |

### 4.3 Token storage

`PLAINTEXT_TOKEN_MODE=True` is active. `bot_token` columns store plaintext. `SecurityService.encrypt()` is a no-op. `SecurityService.decrypt()` handles both `gAAAA%` ciphertext and plaintext for backward compatibility. Live count of encrypted rows: 0.

### 4.4 Migrations

25 migrations in `supabase/migrations/`, all applied to live Supabase. 8 pre-supabase-CLI patches in `docs/history/legacy_migrations/` (historical reference only).

---

## 5. External Interfaces

### 5.1 HTTP API (port 8011, bound to 127.0.0.1)

All non-health endpoints require `X-Monitor-Key` header. Rate limit: 120 req/min per key/IP (Redis-backed). `ENV=production` disables `/docs`, `/redoc`, `/openapi.json`, `/scan/trigger`.

**Health (10 endpoints):** `GET /`, `GET /health/`, `GET /health/detailed`, `GET /health/metrics`, `GET /health/queues`, `GET /health/operational`, `GET /health/circuit-breakers`, `POST /health/circuit-breakers/{name}/reset`, `GET /health/quotas`, `GET /health/bot-pool`

**Monitor (16 endpoints):** `GET /monitor/stats`, `GET /monitor/credentials`, `GET /monitor/messages`, `GET /monitor/findings`, `GET /monitor/findings/{id}`, `GET /monitor/findings/{id}/evidence`, `POST /monitor/findings/{id}/feedback`, `POST /monitor/engagement/lifecycle`, `GET /monitor/export`, `GET /monitor/broadcasts/pending`, `POST /monitor/broadcasts/{id}/retry`, `POST /monitor/topics/revoked/close`, `GET /monitor/webhooks`, `GET /monitor/targets/export`, `GET /monitor/search`, `GET /monitor/operators`

**Ingest:** `POST /ingest/extension/credentials`, `POST /ingest/tokens`

**Scan (dev only):** `POST /scan/trigger` — 403 in `ENV=production`

**Media:** `GET /media/{message_id}` — proxies via source bot token, `Cache-Control: no-store`

**Honeypot:** `POST /honeypot/receive/{credential_id}`, `GET /honeypot/status`

### 5.2 Admin bot commands

`/start`, `/stop`, `/optout`, `/unsubscribe`, `/status`, `/pause`, `/resume`, `/restart`, `/help`, `/commands`, `/bots`, `/telemetry`, `/indicators`, `/getfile`, `/archive`, `/backfill`, `/starthunter` (ConversationHandler), `/cancel`

### 5.3 External integrations

Telegram Bot API, Telegram MTProto (Telethon), Supabase (PostgreSQL + Auth), Redis, GitHub OAuth 2.0, Vercel (frontend hosting), and 21 OSINT scanner APIs (all optional, degrade to `[]` when key absent).

---

## 6. Security Posture

**Authentication:**
- Monitor API: `X-Monitor-Key`, `hmac.compare_digest` (constant-time)
- Dashboard: GitHub OAuth 2.0 via Supabase; `app_metadata.operator=true` claim required to access any data; signup disabled (`disable_signup=true`); 1 authorized user
- Honeypot: `X-Telegram-Bot-Api-Secret-Token` header
- Admin bot: numeric Telegram user-ID whitelist only (not username-based)

**Supabase RLS:**
- Service-role bypasses all RLS (backend workers only)
- Anon: fully denied on all raw tables and `discovered_credentials_public` view
- Authenticated without `operator` claim: can authenticate but sees no data
- Authenticated with `operator=true`: can read `discovered_credentials_public`, `evidence_redacted`, `engagement_funnel_daily`

**Token storage:**
- `PLAINTEXT_TOKEN_MODE=True` — tokens at rest are plaintext. Fernet infrastructure remains and is toggleable via config.
- Audit logger token-redacts all log output and `audit_logs.details` payloads before persistence.

**Transport:** All Docker ports bound to `127.0.0.1`. No TLS at the stack level; intended to be fronted by Cloudflare Tunnel or reverse proxy.

**Rate limiting:** `slowapi` 120 req/min per key/IP; Redis-backed cross-worker; `ConnectionError` handler prevents slowapi middleware crash on Redis unavailability.

**Known absent:** No IP allowlist on monitor API; no 2FA on Flower beyond `FLOWER_BASIC_AUTH`.

---

## 7. Performance & Scalability

**Broadcast throughput (current deployment):**
- 1 monitor bot, `BROADCAST_MAX_PARALLEL_TOPICS=1`, `BROADCAST_INTER_MESSAGE_DELAY_SECONDS=5.0`, `BROADCAST_BATCH_SIZE=50`
- Adaptive delay: Redis-persisted, clamps between 1.0 and 10.0 s; backs off on `flood_wait`, tightens on clean runs
- Observed: ~10 msg/min sustained without flood_wait
- Scales linearly with monitor bot count

**Token validation:**
- 30 `getMe` calls/10 s global across all 16 validator workers (Redis token bucket)
- 24 h per-token dedup cache eliminates redundant API calls

**Database (live):**
- 184 MB / 500 MB (35.8%) — Supabase free tier
- `exfiltrated_messages`: ~69 k rows post-prune; `audit_logs`: ~150 k rows (7-day retention)
- Composite index `(event_type, timestamp DESC)` on `audit_logs`

**Concurrency model:**
- One `asyncio` event loop per Celery worker process (`get_worker_loop()` in `celery_app.py`)
- All async tasks share the loop via `loop.run_until_complete()`; preserves `asyncio.Lock` semantics and Telethon connection pooling

---

## 8. Non-Functional Behavior

**Error handling:** All FastAPI handlers return generic `{"detail": "Internal error"}` — no stack traces in responses. Broadcast uses typed `BroadcastSendError(reason, retryable, retry_after_seconds)` classification.

**Logging:** stdlib `logging`, `%(asctime)s | %(levelname)s | %(name)s | %(message)s`; container stdout; json-file driver 10 MB × 3 per service. 113 broad-except sites emit `logger.debug(f"[suppressed] {exc}")`.

**Retry policy:** `retry_with_backoff` in `app/utils/http_client.py` (exponential; handles 429 + 5xx + network errors). Celery task soft limit: 1200 s; hard: 1800 s. Broadcast retry: adaptive delay + `MAX_BROADCAST_ATTEMPTS=8` permanent-fail cap.

**Healthchecks:**
- `api`: Python `urllib.request.urlopen`, interval 30 s, timeout 45 s
- `bot`: `/tmp/bot_alive` file touched every 10 s; stale > 60 s = unhealthy
- Workers: Redis `r.ping()`, interval 60 s, timeout 180 s, start_period 120 s
- `flower`: TCP connect port 5555, interval 30 s, timeout 5 s
- `frontend`: `node -e "require('http').get(...)"`, interval 30 s, timeout 15 s

**Graceful shutdown:** `worker_shutdown` signal closes persistent event loop + Telegram notification. FastAPI lifespan sends shutdown notice on daemon thread. Bot listener: SIGINT/SIGTERM sets stop event.

**Canary:** `flow.canary_findings_check` (hourly) — inserts synthetic finding, verifies, cleans up. Does not require `ENABLE_RAW_MESSAGE_BROADCAST`.

---

## 9. Known Limitations

1. **Single monitor bot** — broadcast rate ~10 msg/min. Add more bots to `MONITOR_BOT_TOKEN` to scale. `BROADCAST_MAX_PARALLEL_TOPICS` should equal bot count.
2. **No Telethon session account** — "No usable user session" broadcast failures for media-archive messages until `/starthunter` is run to add a session account.
3. **No DATABASE_URL** — DDL migrations must be applied via Supabase Management API or SQL editor; `psql`/`supabase db push` unavailable without the Postgres DSN.
4. **Plaintext token storage** — `PLAINTEXT_TOKEN_MODE=True`. Operator accepted this risk. Fernet path is toggleable.
5. **Supabase free tier** — 500 MB DB cap; current 35.8%. Egress resets monthly; prune broadcast-delivered messages older than 30 days if DB grows.
6. **`keepalive_logs` legacy table** — exists alongside canonical `keepalive_log` (singular). No code writes to the plural one; artifact from early migrations.
7. **Pre-existing CI ruff delta** — ruff check now returns 0 locally. CI uses `ruff==0.1.9`; 5 issues in `app/` with that version are noqa-annotated.
8. **Opt-in live test coverage** — the standard suite passes 398 tests and skips 4 live/load probes. The former stats error-hygiene ordering failure is fixed by isolating Redis cache state per test; the honeypot gate tests also pass in the full suite.
9. **Duplicate Vercel project** — a `frontend` project was accidentally created alongside the canonical `theprawnhunter` project. Delete it from the Vercel dashboard.
10. **GitHub OAuth secrets in session history** — the OAuth client ID and secret were posted in a chat session. Regenerate the client secret at github.com/settings/applications.
