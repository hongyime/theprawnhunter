# Codebase Audit

## 0. Run Metadata

| Field | Value |
|---|---|
| Scope | LIVE (Docker stack running, Redis + Supabase read-only diagnostics) |
| Agent capability | shell, file-read, directory-list, network, live DB/Redis read-only |
| Commit SHA | `3a033cfe06123627c37cbfb03662a9a543c61ebf` (branch `main`) |
| REPO_MAP.md used? | Yes — treated as starting hypothesis, verified against code |
| Paths excluded | `frontend/package-lock.json`, `package-lock.json` (npm lockfiles); `.playwright-mcp/page-*.yml` (recorded transcript); no submodules or vendored dirs |
| Phases completed | 0, 1, 2, 3, 4, 5, 6, 7, 8 |
| Total findings | 69 |
| By severity | P0: 4 · P1: 11 · P2: 26 · P3: 28 |
| By category | SEC: 6 · DATA: 7 · CONC: 4 · INTR: 5 · LOGIC: 7 · PERF: 6 · REL: 6 · FE: 2 · FS: 3 · DRIFT: 8 · DEAD: 8 · STRUCT: 7 |

Overriding LIVE finding: **the running Docker images are 8 days out of date and lack the security fixes present on HEAD**. See DRIFT-001 (P0). No unbackupped destructive action was proposed or taken.

---

## 1. Filesystem Health

Filesystem sweep passed with no CRITICAL corruption. `python -m compileall` on `app/`, `scripts/`, `tests/` completed with zero errors. All tracked YAML, TOML, and non-lockfile JSON parses. Both `package-lock.json` files use the npm-standard `""` root key which PowerShell's `ConvertFrom-Json` rejects without `-AsHashtable`; both parse successfully under Python and the correct PowerShell flag.

**No** backup, temp, `.orig`, `.rej`, `.swp`, `~`, sync-conflict, or timestamp-suffixed duplicate files are tracked.  
**No** zero-byte tracked files.  
**No** null bytes in tracked source files.  
`docker-entrypoint.sh` has LF line endings; the `Dockerfile` still runs `sed -i 's/\r$//' /app/docker-entrypoint.sh` at build time (safety net for CRLF pushes from Windows).

Findings: **FS-001**, **FS-002**, **FS-003** (see §8).

---

## 2. Master Feature Map

The canonical record of what the codebase actually does. All references target commit `3a033cfe`. This is the source of truth for §§ 4–12.

### 2.1 API layer — `app/api/`

- **`app/api/main.py`** builds the FastAPI application (`app = FastAPI(...)` at line 94). Startup / shutdown hooks fire a `BroadcasterService.send_log(...)` notice on a daemon thread so the ASGI lifespan cannot be blocked by Telegram. CORS is explicit-allowlist (no wildcard), `allow_credentials=False`, `allow_methods=["GET","POST"]`, `allow_headers=["Content-Type","X-Monitor-Key","Accept"]`. `EXTRA_CORS_ORIGINS` env adds custom origins. `slowapi` rate limiting is Redis-backed (`_rate_key` derives buckets from either a validated `X-Monitor-Key` via `hmac.compare_digest` or the remote IP). Routers included: `monitor`, `scan`, `ingest`, `health`, `media` (prefix `/media`), `honeypot`. `/docs`, `/redoc`, `/openapi.json`, and `POST /scan/trigger` are disabled when `ENV=production`.
- **`app/api/routers/health.py`** (9 endpoints): `/health/` liveness (open), `/health/detailed`, `/health/metrics`, `/health/queues`, `/health/operational`, `/health/circuit-breakers`, `POST /health/circuit-breakers/{name}/reset`, `/health/quotas`, `/health/bot-pool` — all monitor-key gated via `require_monitor_key`. `/health/operational` reads `audit_logs` for canary, broadcast failure counts, scrape reason distribution over `OPERATIONAL_REPORT_WINDOW_HOURS`.
- **`app/api/routers/monitor.py`** (16 endpoints, all monitor-key gated): `/monitor/stats` (30 s in-process cache backed by RPC `get_monitor_stats()`); `/monitor/credentials` with whitelisted `sort_by` (`created_at`, `updated_at`, `collection_yield_score`, `confidence_score` legacy alias, `chat_member_count`); `/monitor/messages`; `/monitor/findings`, `/monitor/findings/{id}`, `/monitor/findings/{id}/evidence`, `POST /monitor/findings/{id}/feedback` (RPC `record_finding_feedback_service`); `POST /monitor/engagement/lifecycle` (pseudonymised via HMAC); `/monitor/export` (JSON/CSV); `/monitor/broadcasts/pending`, `POST /monitor/broadcasts/{id}/retry`; `POST /monitor/topics/revoked/close`; `/monitor/webhooks`; `/monitor/targets/export`; `/monitor/search` (Postgres FTS over messages, `pg_trgm` indexed); `/monitor/operators` (C2 operator clusters).
- **`app/api/routers/scan.py`** (`POST /scan/trigger`): monitor-key gated, 403 in production; source whitelist `{shodan, fofa, github, gitlab, urlscan, sourcegraph, searchcode}`. Enqueues `scanner.scan_<source>` via `celery_app.send_task`, notifies via broadcaster. Exceptions never echo raw traceback (protects Redis DSN / secrets).
- **`app/api/routers/ingest.py`** (`POST /ingest/extension/credentials`, `POST /ingest/tokens`): monitor-key gated. Encrypts on the server before write (`security.encrypt`), token-hash dedup, own-bot guard via `_is_own_bot_token`, kicks off `enrich_credential.delay(new_id)` after insert.
- **`app/api/routers/media.py`** (`GET /media/{message_id}`): monitor-key gated. Looks up `exfiltrated_messages.file_meta.file_id`, decrypts the source bot token, uses `python-telegram-bot` `bot.get_file(...)` to stream media, responds with `Cache-Control: no-store`. Exception paths never leak the token in the response.
- **`app/api/routers/honeypot.py`**: `POST /honeypot/receive/{credential_id}` receives Telegram push webhooks from taken-over bots. Fail-closed if `HONEYPOT_MODE=False`; validates `X-Telegram-Bot-Api-Secret-Token`; checks per-credential allowlist (`AUTO`, explicit UUIDs, or default-deny). Inserts payload into `honeypot_updates`, fires an async `dispatch_alert`. Always returns HTTP 200 so Telegram does not retry. `GET /honeypot/status` returns config state, gated by an inline monitor-key compare (see SEC-002).

### 2.2 Core layer — `app/core/`

- **`config.py`** (`Settings(BaseSettings)`): 79+ fields (HEAD). Loads `.env` from repo root. Validators: `SUPABASE_URL` (must be `http(s)://`), `REDIS_URL` (`redis(s)://`), `ENCRYPTION_KEY` (exactly 44 chars), `PSEUDONYMIZATION_KEY` (≥ 32 chars when set). `MONITOR_BOT_TOKEN` is comma-split and each token is format-validated (`digits:secret`), duplicate `bot_id` collapsed. `GH_OSINT_TOKEN` is aliased into `GITHUB_TOKEN` (GitHub Actions reserves the secret name `GITHUB_TOKEN`).
- **`database.py`**: singleton Supabase client using `SUPABASE_SERVICE_ROLE_KEY` (bypasses RLS). Exports `db`. `DatabaseHealth.check_connection` for `/health/detailed`.
- **`security.py`** `SecurityService`: uses `Fernet` when only `ENCRYPTION_KEY` is set, `MultiFernet(primary, *legacy)` when `ENCRYPTION_KEY_LEGACY` is populated. `.rotate(token)` returns re-encrypted-under-primary; no-op on single-key mode. `.decrypt` tries primary then legacy chain.
- **`auth.py`** `require_monitor_key`: FastAPI dependency; 503 if `MONITOR_API_KEY` is unset (fail-closed), 403 on missing/wrong key. Uses `hmac.compare_digest`. Returns a stable HMAC-derived actor UUID for audit trails.
- **`audit.py`** `AuditLogger.log(event_type, credential_id=None, user='system', details=None, success=True)`: token-redacts `details` via `_TOKEN_RE` and `_TOKEN_URL_RE` before logging and persisting. Persists high-importance events (`TOKEN_DECRYPTED`, `TOKEN_REVOKED`, `CREDENTIAL_CREATED`, `SCRAPE_*`, `BROADCAST_FAILED`, `CANARY_FLOW_CHECK`, `WEBHOOK_*`, `TOPIC_CLOSED`, `TASK_FAILURE`) to `audit_logs`. Uses a daemon thread from async contexts to avoid unawaited-task warnings on short-lived loops. Missing-table error is throttled to once / 5 min.
- **`circuit_breaker.py`** per-service breakers (`shodan`, `urlscan`, `github`, `fofa`, dynamic get for others). `/health/circuit-breakers` reads state; `POST .../reset` clears one.
- **`redis_srv.py`** `RedisService`: lazy client, cooldown API (`set_cooldown`, `is_on_cooldown`), rotation-index counter, per-owner lock (`acquire_lock`, `release_lock` via **Lua CAS** to prevent stealing), incremental counters with TTL. Async wrappers `get_cached_getme` / `set_cached_getme`.
- **`retry.py`** `@retry` sync/async decorator. `db_retry.py` `with_db_retry` wraps DB calls with backoff.
- **`connectivity.py`** `check_internet`, `wait_for_internet_sync/async` — gates task start on TCP reachability of Telegram's Bot API host.
- **`queue_monitor.py`** — `record_task_enqueued`, `record_task_started` write timestamps to Redis; `/health/queues` reads oldest-job age per queue.
- **`webhook.py`** `dispatch_alert(payload)`: POSTs to `ALERT_WEBHOOK_URL` with `X-Webhook-Secret`. `ENABLE_LEGACY_EVENT_ALERTS` (default False) gates opaque per-event alerts.
- **`metrics.py`** in-memory counters exposed at `/health/metrics`.
- **`constants.py`** `LOCK_TTL_SECONDS = 120`, `CLAIM_TIMEOUT_MINUTES = 15`, `MAX_ERRORS_BUFFER`, misc.
- **`logger.py`** `get_logger` wrapper; JSON handler via stdlib `logging`. Logs go to container stdout (json-file driver rotates at 10 MB × 3 per service — see `docker-compose.yml`).

### 2.3 Services — `app/services/`

- **`scanners.py`** + **`scanners_extension.py`** — 21 scanner classes (`ShodanService`, `FofaService`, `UrlScanService`, `GithubService`, `GitlabService`, `ExaService`, `WaybackService`, `CommonCrawlService`, `SourcegraphService`, `GithubGistService`, `GrepAppService`, `PublicWwwService`, `GoogleSearchService`, `BitbucketService`, `PastebinService`, `RentryService`, `HastebinService`, `NetlasService`, `ReplitService`, `PostmanService`, `SearchcodeService`). Shared regex `TOKEN_PATTERN = r'(?<![A-Za-z0-9])(?:bot)?(\d{8,15}:[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])'` (handles `/bot<token>` URL form via negative lookbehind). `_is_valid_token` rejects Fernet ciphertext, pure hex, and leading-zero bot IDs. Each service returns `list[{"token": ..., "meta": {...}}]`, degrades to `[]` when its API key is absent.
- **`scraper_srv.py`** + **`_scraper/*.py`** — `ScraperService.scrape_history(bot_token, chat_id, limit=3000)` orchestrates four strategies in `_scraper/strategies.py`: `BotApiUpdateReader` (Bot API `getUpdates`, provides anchor ID), `TelethonHistoryReader` (`iter_messages`), `MessageIdReader` (bruteforce backwards from anchor), `ForwardingArchiveReader` (join then forward — last resort via `UserAgentJoinService`). `BotPreflightService` classifies invite constraints. `WebhookStateService` optionally calls `deleteWebhook` (only if `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE=True`). `ScrapeResultClassifier` maps outcomes to `ScrapeReason` codes; the monitor-group is refused as a victim chat (`_is_monitor_group`).
- **`broadcaster_srv.py`** — `BroadcasterService.send_message(group_id, thread_id, msg_row)`. Round-robin across `MONITOR_BOT_TOKEN` tokens via `itertools.cycle`. `_classify_broadcast_exception` maps `RetryAfter`, `TimedOut`, `Forbidden`, `BadRequest` (`topic_deleted`, `message thread not found`) to `BroadcastSendError(reason, detail, retryable, retry_after_seconds)`. `_media_filename` handles APK MIME types explicitly (registers `.apk/.apks/.xapk` as `application/vnd.android.package-archive`). `send_log(msg)` posts operator notices; `ensure_topic`, `rename_topic` manage forum topics.
- **`bot_listener.py`** — `python-telegram-bot` polling loop, 1 381 lines. `main()` at line 1544 sweeps orphan `temp_login_*.session*` files, parses comma-separated `MONITOR_BOT_TOKEN` into a per-bot task pool. Distributed poll lock via Redis (`INSTANCE_ID`, `LOCK_TTL_SECONDS=120`). Handlers: `start`, `stop`/`optout`/`unsubscribe`, `status`, `pause` (sets `system:paused` in Redis), `resume`, `restart`, `help`/`commands`, `bots`, `telemetry`/`indicators`, `getfile`/`archive`, `backfill`. `ConversationHandler` for `/starthunter`: `WAIT_PHONE → WAIT_CODE → WAIT_PASSWORD`, 180 s timeout, orphan-temp-session cleanup on every exit path (including timeout). `on_chat_member_update` auto-promotes newly-joined session accounts to minimal admins (only `can_invite_users=True`). `log_update` skips private DMs (avoids logging phone/2FA in stdout) and monitor-group echoes.
- **`user_agent_srv.py`** — `UserAgentService` manages Telethon sessions from `telegram_accounts`. Distributed session lease via `locked_by`, `locked_until` (10 min claim, cleared by `docker-entrypoint.sh` on startup only for expired leases).
- **`bot_manager_srv.py`** — `BotClientManager` pooling of Telethon `TelegramClient` per bot token.
- **`findings.py`**, **`finding_alerts.py`**, **`entities.py`**, **`engagement.py`**, **`telemetry_parser.py`**, **`topic_admin_srv.py`** — support the analyst workflow (persistent Insight Queue) and monitor-group topic admin.

### 2.4 Workers — `app/workers/`

- **`celery_app.py`** (`Celery("telegram_hunter", broker=REDIS_URL, backend=REDIS_URL)`): persistent event-loop per worker (`get_worker_loop`, `_run_sync`); signals `worker_ready`, `worker_shutdown`, `task_failure`, `before_task_publish`, `task_prerun`. Task routing: `scanner.*`, `firehose.*` → `scanners`; `validation.*`, `pivot.*` → `validation`; `flow.exfiltrate_chat`, `flow.rescrape_active` → `scrape`; everything else → `celery`. Beat schedule contains ≈ 40 entries covering broadcast, exfil, canary, webhook probe + takeover, findings production, entity graph, honeypot redirect touches 2/3, C2 operator clustering, media hashing, weekly attribution, per-scanner cadences, retry-cold, and audit tasks.
- **`flow_tasks.py`** — 3 864 lines, 33 `@app.task` handlers. Highlights: `flow.exfiltrate_chat` (self-heal for raw tokens, revoke on permanent Telethon errors, upsert into `exfiltrated_messages` with `on_conflict='credential_id,telegram_msg_id'`, upserts `telemetry_indicators`); `flow.broadcast_pending` (Redis lock `telegram_hunter:lock:broadcast` TTL=120s with 90s renewer thread; DB-level atomic claim via `broadcast_claimed_at`; stale-claim reclaim after `CLAIM_TIMEOUT_MINUTES=15`; topic recreation on `topic_deleted`); `flow.enrich_credential` (get_dialogs, forum-topic creation, meta write); `flow.canary_flow_check` (synthetic DB → broadcast → optional frontend poll); `flow.probe_webhooks` + `flow.force_webhook_takeover_pass` + `flow.pin_webhook_url`; `flow.hash_exfil_media` (SHA-256 + `imagehash.phash` for photos); `flow.honeypot_redirect_sweep` + `honeypot_redirect_one` (guarded by both `HONEYPOT_REDIRECT_MODE` and `HONEYPOT_REDIRECT_AUTHORIZED`); `flow.reconcile_topics_from_db`; `flow.audit_user_agent_group_membership`; `flow.system_heartbeat`; `flow.system_help`; `flow.exfil_latency_report`; `flow.attribution_graph_report`; `flow.cluster_c2_operators`; `flow.media_duplicate_report`; `flow.reclassify_dark_matter`; `flow.produce_findings`, `flow.build_entity_graph`, `flow.route_finding_deltas`, `flow.daily_findings_digest`, `flow.weekly_finding_alerts`, `flow.source_quality_report`.
- **`scanner_tasks.py`** — 22 `scanner.scan_*` handlers plus `scanner.retry_cold`. `_save_credentials_async` enqueues `validation.validate_token` per result on the `validation` queue. `_token_already_validated` uses Redis `validated:recent:<sha256>` with 24 h TTL to soft-dedup. `_is_own_bot_token` hard-drops monitor tokens even if a scanner finds one.
- **`validation_tasks.py`** — `validation.validate_token` (Redis token-bucket `rate_limit:telegram_getMe`, default 30/10 s, capped at `VALIDATE_RATE_MAX_WAIT=30 s`); `validation.refresh_pending_tokens` (cursor-paginated via Redis `refresh_pending:cursor`); `validation.backfill_scoring`.
- **`pivot_tasks.py`** — `pivot.search_github_user`, `pivot.search_bot_username`, `pivot.search_webhook_host` — fanned out from every successful `getMe`.
- **`firehose_tasks.py`** — `firehose.poll_github_events` — ETag-aware GitHub public event polling every 30 s.
- **`audit_tasks.py`** — `audit.audit_active_topics`, `system.self_heal`, `system.enforce_whitelist`, `system.cleanup_general_topic`, `audit.prune_audit_logs`, `audit.cleanup_matkap_bots`, `system.backfill_general_messages`.
- **`import_tasks.py`** — `system.import_csv`: recovers `.pending` back to `.csv` on startup (interruption-safe), atomically claims `.csv → .pending`, parses, calls `_save_credentials_async` via `csv_import` source, moves to `imports/processed/`.
- **`honeypot_redirect_tasks.py`** — `flow.honeypot_redirect_touch2`, `touch3`, `flow.honeypot_proactive_outreach`.

### 2.5 Storage — `database/` + `supabase/migrations/`

Canonical schema in `database/init.sql`: tables `discovered_credentials`, `exfiltrated_messages`, `monitor_stats` (singleton), `telemetry_indicators`, `telegram_accounts`, `audit_logs`, `keepalive_log`. Triggers `trg_monitor_stats_credentials_delta` / `trg_monitor_stats_messages_delta` maintain the aggregate row; `get_monitor_stats()` is `SECURITY DEFINER` granted only to `service_role`.

RLS (`database/rls_policies.sql`): service-role bypasses; raw `discovered_credentials` reads denied for anon; `INSERT`/`UPDATE` allowed for anon **only** when `request.headers ->> 'x-extension-secret'` matches the DB parameter `app.extension_write_secret`. `exfiltrated_messages` fully revokes anon and authenticated on the raw table; authenticated operators must use the `evidence_redacted` view which HMAC-pseudonymises senders and regex-masks token strings before truncating content to 500 chars. `discovered_credentials_public` view is granted **only** to `authenticated`; anon is fully revoked from that view too.

21 dated migrations in `supabase/migrations/`: broadcast reliability columns, `broadcasted_at`, message FTS index, `media_hashes`, `honeypot_updates`, `system_state`, `account_membership_admin`, `sender_user_id`, `honeypot_redirect_log`, `monitor_stats`, multi-touch redirect columns, Supabase optimisation, `collection_yield_score`, RLS hardening, `discovered_credentials_public` grants, retention-job disable, `findings` + `finding_evidence` (insight queue), `entities` + `engagement_events`, `finding_alert_policies`, `monitor_findings_feedback`, `dashboard_operator_authorization`.

### 2.6 Frontend — `frontend/`

Next.js 16.2.4, React 19.2.3, TypeScript 5, Tailwind CSS 4, `@supabase/supabase-js ^2.89`. `app/page.tsx` composes four views (`findings`, `chat`, `botTelemetry`, `globalTelemetry`) with `useAuth` in `lib/auth.tsx` gating access. `lib/supabase.ts` reads `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_KEY` (anon key baked into the client bundle). Vitest suite covers `page.tsx`, `signin/page.tsx`, `ChatWindow`, `FindingsQueue`, `auth.tsx`.

### 2.7 Extension — `extension/`

Manifest V3, `service_worker: background.js`, content script `content.js` on `fofa.info` and `en.fofa.info`. `background.js` scans 49 country codes × two domains, validates tokens via `api.telegram.org/bot<token>/getMe`, keeps `state.results` capped at 300 entries (5 MB chrome.storage limit). Upload path: `/ingest/extension/credentials` when configured, else direct Supabase REST insert (writes **raw** tokens under the extension-secret RLS policy — see SEC-001).

### 2.8 Deployment — Docker Compose

`docker-compose.yml` (10 services): `redis` (7-alpine, appendonly, 1 GB maxmemory, `noeviction`); `api` (gunicorn -w 4, uvicorn worker, timeout 120, healthcheck via `urllib.request.urlopen`); `worker-core` (`-Q celery --concurrency=8`); `worker-scanners` (`-Q scanners --concurrency=8`); `worker-scrape` (`-Q scrape --concurrency=6`); `worker-validators` (`-Q validation --concurrency=16`); `beat` (schedule file at `/app/beat/celerybeat-schedule`); `bot` (`python -m app.services.bot_listener`, healthcheck touches `/tmp/bot_alive` every 10 s, container marked unhealthy if file older than 60 s); `flower` (refuses to start unless `FLOWER_BASIC_AUTH` differs from `admin:changeme`); `frontend` (Next.js standalone build, `NEXT_PUBLIC_*` baked at build time). All ports bound `127.0.0.1`. Workers use a Python-based `redis.ping()` healthcheck (avoids the false-positive `celery inspect ping` issue documented inline).

`docker-compose.prod.yml` overrides `ENV=production`, `DEBUG=False`, and lets concurrency/timeouts/worker-count come from env vars.

`docker-entrypoint.sh` sweeps expired `telegram_accounts.locked_until` on startup (scoped — does **not** clear live leases; earlier version cleared all leases, that regression is fixed).

External volumes retain the legacy prefix `telegramhunter_*` for `redis_data`, `sessions`, `imports`, `beat_schedule`.

### 2.9 Configuration surface

Complete env-var table appears in `REPO_MAP.md` §10 (same commit). Highlights that matter for this audit:

- `MONITOR_API_KEY`: **required** (no default), fail-closed 503 if unset.
- `ENCRYPTION_KEY`: 44 chars mandatory.
- `MONITOR_BOT_TOKEN`: comma-separated, each `digits:secret`.
- `ENABLE_RAW_MESSAGE_BROADCAST` (HEAD): default `False` — `flow.broadcast_pending` early-returns if unset. **Not present in the running image** (see DRIFT-001).
- `HONEYPOT_REDIRECT_MODE` (HEAD default `True`) + `HONEYPOT_REDIRECT_AUTHORIZED` (HEAD default `False`) — both must be True for outgoing redirects to send.
- `FLOWER_BASIC_AUTH`: refuses `admin:changeme`; must be set.
- `EXTENSION_WRITE_SECRET`: stored **only** in the Supabase DB parameter `app.extension_write_secret`, never in env.
- 19 optional scanner keys, all degrading to `None`.

### 2.10 Entry points

| Entry point | Path | Trigger |
|---|---|---|
| FastAPI | `app/api/main.py:94` | `gunicorn ... uvicorn.workers.UvicornWorker -w 4` |
| Celery worker (×4 queues) | `app/workers/celery_app.py:32` | `celery -A app.workers.celery_app worker -Q <q>` |
| Celery beat | same | `celery -A ... beat --schedule /app/beat/celerybeat-schedule` |
| Bot listener | `app/services/bot_listener.py:1601` → `main()` at 1544 | `python -m app.services.bot_listener` |
| Flower | `app/workers/flower_app.py:14` | `celery -A app.workers.flower_app flower --port=5555` |
| Frontend | `frontend/app/page.tsx:1` via `node server.js` | Next.js standalone runtime |
| CSV import sweep | `docker-entrypoint.sh:20-30` | container entrypoint pre-marks `.csv` → `.pending` |
| Validators | `scripts/validate_deployment.py:189`, `scripts/validate_startup.py:135` | manual `python scripts/...` |
| Extension worker | `extension/background.js:1` | `chrome.action` popup / `chrome.alarms` |

---

## 3. Reconciliation Summary

**Truth gap** — of the features stated in `PRD.md`, `README.md`, and the historical `bugfix.md`/`design.md`/`tasks.md`, roughly:

- **Fully implemented and matching docs**: ≈ 75 % (scanner pipeline, validation queue, Fernet with rotation, `broadcast_pending` distributed claim, monitor API with `require_monitor_key`, canary flow, honeypot receive, forum-topic management, CSV import, RLS + service-role model, redacted evidence view).
- **Implemented but drifts from docs**: ≈ 15 % — see §6. Includes the whole findings/entity/engagement pipeline (implemented, not in `PRD.md` or `README.md`).
- **Documented but missing / broken**: ≈ 5 % — see §4.
- **Historical / obsolete docs still shipping**: ≈ 5 % — `AUDIT.md`, `AUDIT_LOG.md`, `security_audit.md` (2026-05-24), `bugfix.md`, `design.md`.

**State of the system.** The codebase is architecturally sound: multi-queue Celery with a persistent event loop per worker, Redis-lock + DB-claim broadcast atomicity, Fernet + `MultiFernet` key rotation, structured RLS with a redacted view for authenticated operators, deterministic ID-addressable audit events, canary flow. It has a working live deployment with 345 528 exfiltrated messages, 344 054 broadcasted, and a healthy 10-service stack. The critical concerns are not architectural — they are (1) **deployment drift**: the running images are 8 days behind HEAD and lack recent security patches (DRIFT-001), (2) **data-at-rest exposure**: 47.8 % of sampled credential rows carry plaintext bot tokens (SEC-001), (3) **applied-migration gap**: four tables referenced by shipping code (`findings`, `finding_evidence`, `engagement_events`, `honeypot_redirect_log`) are absent from the live Supabase (DATA-001), and (4) **audit-log bloat**: 357 357 rows with `broadcast.failed` dominating recent inserts (DATA-002 / DATA-003).

**Production readiness: 8 / 17 PASS, 3 PARTIAL, 6 FAIL.** See §11.

---

## 4. Critical Gaps — Documented but Unimplemented

| Feature | Doc source | Severity | Why it matters |
|---|---|---|---|
| `SerperService` scanner | `.env.template` `SERPER_API_KEY=`; `README.md` scanner-keys table (see git history) | P3 | Class was removed (`app/services/scanners.py:841` comment: "Replaces SerperService"), the env key remains. New operators may set `SERPER_API_KEY` expecting it to work. |
| `CENSYS_ID`, `CENSYS_SECRET`, `HYBRID_ANALYSIS_KEY` | `app/core/config.py:130-132` | P3 | Fields declared, no scanner references them. `POST /scan/trigger` already rejects `censys` / `hybrid` (`app/api/routers/scan.py:31`). |
| Beat entries `scan-fofa-4hours`, `scan-gitlab-6hours`, `scan-pastebin-12hours`, `scan-replit-12hours`, `scan-google-12hours` | `app/workers/celery_app.py` inline comments | P3 | The tasks exist and the classes exist but the beat entries are commented out — a doc-side reader will assume they run. |
| Frontend RLS access to `discovered_credentials_public` for anon | `frontend/README.md` "GRANT SELECT ... TO anon" | P0 | `supabase/migrations/20260903000005_discovered_credentials_public_authenticated.sql` REVOKEs from anon and GRANTs only to `authenticated`. Vercel and local Docker frontends are still on old SHA (see `tasks.md` P0-005), so unauthenticated access is currently possible until redeploy. |
| Live Supabase tables `findings`, `finding_evidence`, `engagement_events`, `honeypot_redirect_log` | code depends on them across `flow_tasks.py`, `services/findings.py`, `services/engagement.py`, `honeypot_redirect_strategies.py`; migrations `20260904000002/000003/000004` and `20260806000001` | P0 | Verified missing in live Supabase (Phase 3). `/monitor/findings` returns 500 (`PGRST205 Could not find the table 'public.findings' in the schema cache`). Multiple beat tasks (`flow.produce_findings`, `flow.build_entity_graph`, `flow.route_finding_deltas`, redirect touches 2/3) no-op or fail. |

---

## 5. Ghost Features — Implemented but Undocumented (or Under-documented)

| Ghost | Path | What it does |
|---|---|---|
| `/ingest/tokens` | `app/api/routers/ingest.py:212` | Plain-text / JSON-array token paste; monitor-key gated. README does not mention it. |
| `/monitor/search`, `/monitor/operators`, `/monitor/broadcasts/pending`, `/monitor/broadcasts/{id}/retry`, `/monitor/topics/revoked/close`, `/monitor/engagement/lifecycle`, `/monitor/targets/export`, `/monitor/webhooks`, `/monitor/export`, `/monitor/findings/*` | `app/api/routers/monitor.py` | 10 endpoints beyond the four described in `README.md`. |
| Findings / entities / engagement pipeline | `app/services/findings.py`, `entities.py`, `engagement.py`, tasks `flow.produce_findings`, `flow.build_entity_graph`, `flow.route_finding_deltas`, `flow.daily_findings_digest`, `flow.weekly_finding_alerts`, `flow.source_quality_report`, `flow.attribution_graph_report` | Entire priority-first analyst queue and entity graph — not mentioned in `PRD.md`. |
| Honeypot dual gate | `app/core/config.py:36-46`, `flow_tasks.py:honeypot_redirect_one` | `HONEYPOT_REDIRECT_MODE` alone is insufficient; `HONEYPOT_REDIRECT_AUTHORIZED` must also be True. README omits this. |
| Multi-touch honeypot redirects | `app/workers/tasks/honeypot_redirect_tasks.py`, beat entries `honeypot-redirect-touch2-daily`, `touch3-daily`, `honeypot-proactive-outreach-6h` | Daily follow-ups + inline-mode proactive outreach — undocumented. |
| Media perceptual-hash pipeline | `flow.hash_exfil_media`, `flow.media_duplicate_report`, `media_hashes` table (`supabase/migrations/20260803000011_media_hashes.sql`) | Cross-bot duplicate detection via `imagehash.phash`. |
| C2 operator clustering | `flow.cluster_c2_operators`, `/monitor/operators` | Ranks hosted-service tenants and Shodan orgs. |
| GitHub Events firehose | `app/workers/tasks/firehose_tasks.py`, `firehose.poll_github_events` beat entry (every 30 s) | ETag-aware public event polling, real-time leak detection. |
| Pivot fan-out | `app/workers/tasks/pivot_tasks.py` (`pivot.search_github_user`, `search_bot_username`, `search_webhook_host`) | Fires after every successful `getMe`. |
| Fernet key rotation | `app/core/security.py` (`SecurityService.rotate`), `scripts/rotate_credentials.py` | Full `MultiFernet` chain with re-encryption script. Present in `README.md` under "Advanced Features" but not in `PRD.md`. |
| CI security workflows | `.github/workflows/bandit.yml`, `semgrep.yml`, `trufflehog.yml` | Weekly + push-triggered SARIF upload to Code Scanning. |
| DB retention triggers `trg_monitor_stats_*` | `database/init.sql:150-200` | Real-time singleton maintenance — implicit in code. |
| Redacted evidence view (`evidence_redacted`) | `database/rls_policies.sql` | HMAC-pseudonymised, token-masked. Not surfaced in README. |
| `NETLAS_API_KEY_2` (dual-account rotation) | `app/services/scanners_extension.py:NetlasService` | Two-account Netlas rotation with Redis daily counters. `.env.template` lists both but README only shows one. |

---

## 6. Documentation Drift

| Documented behaviour | Actual behaviour | Path | Correction |
|---|---|---|---|
| `PRD.md`: "10 services" appears in `README.md`; PRD's own table lists **7 services** | `docker-compose.yml` has 10 services | `PRD.md:22-33` vs `docker-compose.yml` | Update PRD or delete it — see DRIFT-003 |
| PRD: `worker-core --concurrency=4`, scanners=2, scrape=2 | Code: 8, 8, 6, plus validators=16 | `PRD.md:32-40` vs `docker-compose.yml` | Update PRD |
| PRD: "Celery Beat schedules 25 tasks" | Beat contains ≈ 40 entries | `PRD.md:26`, `app/workers/celery_app.py:220-500` | Update PRD |
| PRD: `broadcast_pending` batch size 100, 2 s sleep | Code: default 200 batch, 1.5 s sleep configurable via `BROADCAST_BATCH_SIZE` | `PRD.md:3.6` vs `flow_tasks.py:1050-1055` | Update PRD |
| PRD: circuit breaker `threshold=5, timeout=60` | Code (`app/core/circuit_breaker.py`) still uses `threshold=3, timeout=300` when no override — `bugfix.md` BUG-016 open | `PRD.md:3.11`, `circuit_breaker.py` | Either update PRD or align code |
| `README.md`: "**383 tests** (317 unit + 7 integration + 59 top-level)" | File-level count is 46 unit + 3 integration + 1 load + 8 top-level = 58 test files; function-level not verified this run | `README.md:245` | Re-count with `pytest --collect-only` |
| README: `CANARY_EXPECTED_TEXT` default `TheprawnHunter-canary` | Code default is `telegramhunter-canary` (lower-case) | `README.md:66`, `app/core/config.py:78` | Update README |
| README: "`MONITOR_API_KEY` — If set, all `/monitor/*` and `/health/detailed` require it" | Field is **required** (no default). `require_monitor_key` returns 503 when unset | `README.md:42`, `app/core/config.py:46`, `app/core/auth.py:39-44` | Update README |
| `SUPABASE_KEEPALIVE_SETUP.md`: `keepalive_logs` (plural) | Code + `database/init.sql` use `keepalive_log` (singular) | `SUPABASE_KEEPALIVE_SETUP.md:9`, `database/init.sql:316` | Rewrite that doc |
| `AUDIT.md`, `AUDIT_LOG.md`, `security_audit.md` (2026-05-24) claim 3 680 source files, "N/A personal project" | Tracked file count is 286; project is a production stack | root files | Delete / archive |
| `bugfix.md` marks BUG-004 through BUG-016 open | Most items were fixed by the round-2 remediation. `git log` shows commits like `8986581 fix(api): rate limit bucket bypass`, `2350608 fix(security): add is_dashboard_operator()`, `8dadb95 fix(security): honeypot dual-gate` | `bugfix.md`, `git log` | Retire the file |
| `tasks.md` P0-005 (open, HIGH-security) — "Frontend release drift exposes findings without auth" | Verified LIVE: worker/api/frontend images created 2026-08-28, HEAD is 2026-09-05 — 8 days of unshipped fixes | `tasks.md`, `docker inspect` | Ship the deploy |
| README mentions `MONITOR_GROUP_ID` and `/starthunter` as the account-login flow | Confirmed. Additional gate `ALLOW_PUBLIC_STARTHUNTER` (default False) not documented — private-DM whitelisted admin only | `README.md`, `app/core/config.py:31` | Add to README |
| PRD: "Named volumes: `redis_data`, `sessions`, `imports`" | Compose lists four externals (adds `beat_schedule`) with legacy `telegramhunter_*` prefix | `PRD.md`, `docker-compose.yml` | Update PRD |
| README env table lists `BITBUCKET_USER + BITBUCKET_API_TOKEN` and doesn't reference the old `BITBUCKET_APP_PASSWORD` | Matches code. Historical `bugfix.md` CONFIG-002 already resolved | `README.md` | No action |

Findings register entries: **DRIFT-001** … **DRIFT-008**.



---

## 7. Data Integrity

Live SELECTs run against Supabase via the running `worker-core` container using the service-role key. No writes performed.

| Table / View | Row count (live) | Schema match | Anomalies | Recommendation |
|---|---:|---|---|---|
| `discovered_credentials` | 2 119 | **MISMATCH** — code depends on `collection_yield_score` generated column (migration `20260903000003`). Present in DB (`get_monitor_stats` RPC returns 2 119 / 938). | 478 / 1 000 sampled rows (**47.8 %**) store plaintext `bot_token` (no `gAAAA` Fernet prefix). All are `status=revoked`. Breakdown: `github` 252, `csv_import` 139, `manual_import` 56, `shodan` 31. Median length 46 chars = raw Telegram token. | See SEC-001 |
| `exfiltrated_messages` | 345 528 | Match. `broadcast_error`, `next_retry_at`, `broadcast_claimed_at`, `broadcasted_at` columns present. | 1 474 unbroadcasted, 1 478 with `broadcast_error != NULL` and `next_retry_at != NULL`. Dominant failure reason in recent audits: `media_archive_not_found`. Two rows are `broadcast_claimed_at != NULL` and `is_broadcasted=false` — stale claims below the 15-min threshold. FK integrity to `discovered_credentials` verified on 200 sampled `credential_id` values — **0 orphans** (CASCADE constraint enforced). | See DATA-003 |
| `monitor_stats` (singleton) | 1 | Match. `credentials_total=2 119`, `credentials_active=938`, `messages_exfiltrated=345 528`, `messages_broadcasted=344 054`. Values equal actual counts. | None — the maintaining triggers `trg_monitor_stats_*` and RPC `get_monitor_stats()` return consistent numbers. | Keep monitoring; refresh timestamp `2026-09-05T16:30:41Z` was fresh at query time. |
| `audit_logs` | 357 357 | Match. `idx_audit_event_type`, `idx_audit_timestamp` present. | Table is bloated. In the most-recent 1 000 rows, event-type distribution is: `broadcast.failed` 731, `scrape.strategy_attempt` 188, `scrape.classified` 49, `topic.closed` 14, `task_permanent_failure` 8, `webhook.takeover` 7, `canary.flow_check` 3. `audit.prune_audit_logs` is scheduled weekly but retains 90 days — insertion rate exceeds the prune window. | See DATA-002 |
| `telegram_accounts` | 4 | Match. | Small pool; verify all rows have valid `session_path`, `phone`, `status='active'`, and no stale `locked_until`. Beyond this run's read-only scope. | — |
| `honeypot_updates` | 0 | Match (migration `20260803000012` applied). | Never received a webhook — either `HONEYPOT_MODE=False` in `.env`, or no captured bot was set up with our public URL. Consistent with `/honeypot/status` if inspected. | Non-issue if intentional. |
| `telemetry_indicators` | 33 316 | Match. `idx_telemetry_indicators_type_val`, `idx_telemetry_indicators_cred` present. | Growing steadily. | — |
| `media_hashes` | 7 477 | Match. Contains sentinel rows with `sha256='__failed__<id>'` for un-downloadable media. | Sentinel scheme is fragile — see INTR-005. Verify no unique-constraint on `message_id` alone; if two workers race on `hash_exfil_media`, duplicate rows can appear. | See CONC-003 |
| `keepalive_log` | 8 | Match. | Table only pinged once daily by GitHub Actions; small size is fine. | — |
| `findings` | **MISSING** | **FAIL** — `PGRST205 Could not find the table 'public.findings' in the schema cache` | Migration `supabase/migrations/20260904000002_insight_queue.sql` not applied. `/monitor/findings`, `/monitor/findings/{id}`, `/monitor/findings/{id}/evidence`, `POST /monitor/findings/{id}/feedback` return HTTP 500. Beat task `flow.produce_findings` no-ops or errors. | See DATA-001 |
| `finding_evidence` | **MISSING** | **FAIL** — same migration | Same endpoint impacts as above. `_finding_evidence_rows` throws. | See DATA-001 |
| `engagement_events` | **MISSING** | **FAIL** — `supabase/migrations/20260904000003_entities_engagement.sql` not applied | `POST /monitor/engagement/lifecycle` fails; `flow.build_entity_graph`, `flow.route_finding_deltas` cannot function. | See DATA-001 |
| `honeypot_redirect_log` | **MISSING** | **FAIL** — `supabase/migrations/20260806000001_honeypot_redirect.sql` not applied | Multi-touch redirect tasks silently fail; per-user redirect audit trail lost. Redis dedup key still works for basic gating. | See DATA-001 |

**Incomplete-write / interruption sentinels checked (SELECT only):**

- Zero rows have `bot_token IS NULL` or `token_hash IS NULL` in `discovered_credentials`.
- Zero rows have `content = ''` or literal `'undefined'` / `'NaN'` / `'null'` string sentinels in `exfiltrated_messages` (spot-checked on 3 rows).
- Zero rows in `exfiltrated_messages` have `broadcast_claimed_at` older than 15 min AND `is_broadcasted=false` beyond the two already flagged.

Findings register entries: **DATA-001** … **DATA-007**.

---

## 8. Findings Register

Grouped by category (SEC · DATA · CONC · INTR · LOGIC · PERF · REL · FE · FS · DRIFT · DEAD · STRUCT), sorted by severity ascending (P0 first) within each category.

### 8.1 SEC — Security

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| SEC-001 | P0 | CONFIRMED | `database/rls_policies.sql:54-98`, `extension/background.js:*`, live DB sample | 478 / 1 000 sampled rows (47.8 %) store plaintext bot tokens in `discovered_credentials.bot_token`; all are `status=revoked`; the extension direct-write path with `x-extension-secret` writes raw tokens and self-heal only runs during exfiltration, which never happens for tokens that never go active | Route all extension writes through `POST /ingest/extension/credentials` (already server-side encrypts) and remove the anon INSERT/UPDATE policy on `discovered_credentials`; then run `scripts/rotate_credentials.py --encrypt-plaintext` (or equivalent one-off script) against the 478 plaintext rows | L |
| SEC-002 | P1 | CONFIRMED | `app/api/routers/honeypot.py:151-160` | `GET /honeypot/status` compares `x_monitor_key != settings.MONITOR_API_KEY` in-line — not constant-time, inconsistent with `require_monitor_key` elsewhere | Replace inline check with `dependencies=[Depends(require_monitor_key)]` on the route decorator | S |
| SEC-003 | P1 | CONFIRMED | `docker-compose.yml:frontend`, `frontend/vercel.json`, `tasks.md:P0-005` | Vercel + local Docker `frontend` images are running old SHA (per `tasks.md:P0-005`); RLS hardening on `discovered_credentials_public` GRANTs only `authenticated`, but the running frontend bundle still expects anon reads — currently unauthenticated access may be permitted until redeploy | Rebuild and redeploy the frontend container and Vercel project to a HEAD-current commit; confirm anon `SELECT discovered_credentials_public` returns 403 | M |
| SEC-004 | P2 | CONFIRMED | `app/services/scanners.py:54,215,329,430,537,684,796`, `app/services/scanners_extension.py:52,167,226,385`, `app/workers/tasks/flow_tasks.py:1643,1847` | 13 sites disable TLS verification (`httpx.AsyncClient(verify=False)`). Scanner OSINT probes are intentional (random IPs, self-signed), but `flow_tasks.py:1643,1847` are webhook probes — TLS validity is signal-relevant intel | Add an explicit `# nosec` comment plus a `TLS_VERIFY_WEBHOOK_PROBES=True` opt-in for the two `flow_tasks.py` sites so operators can enforce verify by default and record cert-mismatch as intel | M |
| SEC-005 | P2 | POTENTIAL | `app/api/routers/honeypot.py:62` | `POST /honeypot/receive/{credential_id}` reads `await request.json()` with no explicit body-size cap; FastAPI defaults are large. A malicious webhook operator can send oversized payloads that hit Supabase JSONB size limits | Wrap the request read in `try: body = await asyncio.wait_for(request.body(), timeout=5); assert len(body) < 1_048_576` before `json.loads` | S |
| SEC-006 | P3 | CONFIRMED | `app/services/honeypot.py:*` and beat entry `system-enforce-whitelist-6hours` (`app/workers/celery_app.py`) | `bot_listener.py:151` uses `==` for token comparison in one legacy admin-check path (via `_bot_id_from_token`) — non-constant-time but only ID compare, not secret | Replace with `hmac.compare_digest(str(a), str(b))` on the admin path for uniformity | S |

### 8.2 DATA — Data Integrity

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| DATA-001 | P0 | CONFIRMED | Live Supabase; `supabase/migrations/20260806000001_honeypot_redirect.sql`, `20260904000002_insight_queue.sql`, `20260904000003_entities_engagement.sql` | Four migrations not applied: `findings`, `finding_evidence`, `engagement_events`, `honeypot_redirect_log` tables missing. `/monitor/findings/*` returns 500. Analyst workflow endpoints, `flow.produce_findings`, `flow.build_entity_graph`, `flow.route_finding_deltas`, honeypot-redirect multi-touch tasks are all impacted | Apply the four migrations against Supabase (SQL editor or `supabase db push`); verify via `SELECT count(*) FROM findings` etc. | S |
| DATA-002 | P1 | CONFIRMED | `audit_logs` = 357 357 rows; `app/workers/tasks/audit_tasks.py:507` `prune_audit_logs` | Weekly prune retains 90 days but insertion rate (dominated by `broadcast.failed` 731 / 1 000 recent rows) exceeds the retention budget; table grows indefinitely | Reduce `_should_persist` list (exclude `SCRAPE_STRATEGY_ATTEMPT` from persist, or add rate limiting per event_type), and add a composite index `(event_type, timestamp)` if `/health/operational` queries slow down | M |
| DATA-003 | P1 | CONFIRMED | live query on `exfiltrated_messages`; `app/services/broadcaster_srv.py:_media_filename` | 1 478 messages stuck with `broadcast_error` and `next_retry_at` set; dominant reason `media_archive_not_found`. The retry writes `next_retry_at = 24 h out` after 5 attempts, so these will churn forever | Add a `broadcast_attempts >= N → mark permanent-failed` transition (e.g. N=8) so stuck rows drop out of the retry pool; expose a `/monitor/broadcasts/dead-letter` endpoint (or manual clear via existing `POST .../{id}/retry`) | M |
| DATA-004 | P2 | CONFIRMED | `app/workers/tasks/flow_tasks.py:2995-3020` `hash_exfil_media` filter | `not_.is_("file_meta", "null")` includes rows with empty JSONB `{}`. Those rows have no `file_id`, so `broadcaster._download_media_bytes` fails and writes a `__failed__<id>` sentinel — one row per empty-meta message. Grows `media_hashes` with dead entries | Add `.gt("file_meta->>'file_id'", None)` (or a Python-side filter) before download attempt; drop sentinel scheme in favour of an `is_failure BOOLEAN + failure_reason TEXT` pair | M |
| DATA-005 | P2 | POTENTIAL | `app/services/scanners.py:_is_valid_token`, `app/utils/helpers.py:is_valid_telegram_token`, `extension/background.js` | `bugfix.md` BUG-012 flagged token-regex inconsistency across three sites. Current code standardised on `35 chars` in `_is_valid_token`, but the extension regex has not been re-checked in this run | Grep the extension for the regex and align to `\d{8,15}:[A-Za-z0-9_-]{35}` exactly | S |
| DATA-006 | P2 | CONFIRMED | `database/init.sql:298-320` — `audit_logs.details JSONB` | The table has no size cap on `details`. `AuditLogger.log(..., details={"exception": exc_str[:500]})` truncates exception strings but not other detail payloads (e.g. `scrape.strategy_attempt` evidence dicts). One 100 KB payload times N events = table bloat | Enforce a JSON-payload byte cap in `_persist_to_db` (`if len(json.dumps(details)) > 8192: details = {...:'truncated'}`); consider partitioning by month if retention isn't tightened | M |
| DATA-007 | P3 | CONFIRMED | `database/init.sql:316` (canonical) vs `SUPABASE_KEEPALIVE_SETUP.md` | Table name is `keepalive_log` (singular) in canonical schema; the setup doc references `keepalive_logs` (plural). Live table matches code (`keepalive_log` = 8 rows) | Update `SUPABASE_KEEPALIVE_SETUP.md` and any migration numbered `003_add_keepalive_table.sql` if such an artifact was created historically | S |

### 8.3 CONC — Concurrency

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| CONC-001 | P1 | CONFIRMED | `app/workers/tasks/flow_tasks.py:989-1050` | The `_renew_loop` background thread renews the Redis lock every 90 s while the async loop runs. If `broadcast_pending` is invoked from beat and finishes fast (< 90 s), the lock TTL still burns until first renewal; another beat run will skip. Not a correctness issue, but skipping is not surfaced to the operator | Log at INFO whenever the skip occurs (current message is a return string, not logged); consider a Prometheus counter | S |
| CONC-002 | P2 | CONFIRMED | `app/workers/tasks/flow_tasks.py:4030-4070` | `honeypot_redirect_sweep` fires every 30 s. It queues `honeypot_redirect_one` tasks per un-redirected row. Two sweeps overlapping (worker restart + fresh sweep) can dispatch two tasks for the same `honeypot_updates.id` before either finishes and writes `redirected_at`. Redis dedup key `redirect:sent:{cred}:{user}` is set only AFTER successful send | Claim rows atomically at sweep time: `UPDATE honeypot_updates SET redirected_at='pending' WHERE id=? AND redirected_at IS NULL RETURNING id`; process only claimed rows | M |
| CONC-003 | P2 | POTENTIAL | `app/workers/tasks/flow_tasks.py:2963-3060` `hash_exfil_media`, `supabase/migrations/20260803000011_media_hashes.sql` | Two beat runs (30-min cadence) can race: both compute the same set of unhashed candidates and both `INSERT` into `media_hashes`. Migration must add `UNIQUE(message_id)`; not verified in this run because the migration file was not opened | Confirm `UNIQUE(message_id)` on `media_hashes` and add if missing; move the "sentinel on failure" write inside a `INSERT ... ON CONFLICT (message_id) DO NOTHING` | S |
| CONC-004 | P2 | POTENTIAL | `app/core/redis_srv.py:acquire_lock`/`release_lock`, `app/workers/tasks/flow_tasks.py:989` | `RedisService.release_lock` uses Lua CAS by `owner`; `broadcast_pending` in `flow_tasks.py` uses `redis.lock(...)` (redis-py native lock, which also fences by token). Different code paths use different locking primitives — inconsistency that could confuse future edits | Standardise on one lock primitive (the redis-py `redis.lock()` is recommended); or leave a comment header on `redis_srv.RedisService.acquire_lock` documenting the fencing invariant | S |

### 8.4 INTR — Interruption & Recovery

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| INTR-001 | P1 | CONFIRMED | `app/workers/tasks/flow_tasks.py:_broadcast_logic`, `broadcaster_srv.send_message` | Between `broadcaster.send_message` succeeding (Telegram accepts the message) and the DB `UPDATE ... SET is_broadcasted=true`, a worker kill leaves the row claimed but not marked. `CLAIM_TIMEOUT_MINUTES=15` recycles the claim; a second worker re-sends the message. Duplicate broadcast to the monitor group | Persist Telegram's returned message-id after send but before the DB `is_broadcasted=true` write; on retry, check the recorded message-id and skip if present. Alternative: use `EXFIL_MSG_HASH` in the topic message body as a fingerprint for idempotency | M |
| INTR-002 | P2 | CONFIRMED | `app/workers/tasks/flow_tasks.py:honeypot_redirect_one` | Send-then-mark pattern: `HoneypotRedirectStrategies.send_callback_hijack` (or `send_message`) executes, only then `redirected_at` and Redis dedup are set. Worker crash between the two steps re-sends the redirect on next sweep | Set `redirected_at='pending'` before send; on send success flip to `now()`; on failure clear; the pending state prevents re-sends inside a sweep window | M |
| INTR-003 | P2 | CONFIRMED | `app/workers/tasks/import_tasks.py:34-62` | On startup the task recovers `.pending` back to `.csv` unconditionally. If the previous run had already parsed the file and half of the rows were `_save_credentials_async`-enqueued, re-processing the same file re-enqueues those rows. `validation.validate_token` deduplicates via Redis, so no double-write, but wasted Telegram getMe budget | Persist a per-file `imported_at` marker (e.g. a `imports/processed/<name>.done` breadcrumb) and skip `.pending` recovery if the marker exists | S |
| INTR-004 | P2 | CONFIRMED | `app/workers/tasks/flow_tasks.py:_exfiltrate_logic:610-640` | Self-heal encrypt-then-DB-write: on failure of the DB write, the raw token is still used downstream but not persisted encrypted. Interruption between successful send and DB-write can leave DB with raw token. Overlaps SEC-001 | Encrypt-then-persist BEFORE any downstream use; if the persist fails, raise and let the task retry | S |
| INTR-005 | P3 | CONFIRMED | `app/workers/tasks/flow_tasks.py:_hash_exfil_media_logic:3020-3060` | Failure sentinel `sha256='__failed__<id>'` is a fragile string convention with no boolean flag or reason column; a future rename of `message_id` prefix (or migration) breaks the "already failed" filter | Replace sentinel with `is_failure BOOLEAN, failure_reason TEXT` columns on `media_hashes`; migrate existing sentinel rows | M |

### 8.5 LOGIC — Business Logic

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| LOGIC-001 | P1 | CONFIRMED | `app/workers/tasks/flow_tasks.py:canary_flow_check:1413`, `settings.ENABLE_RAW_MESSAGE_BROADCAST` | HEAD default is `False`; when so, the canary returns `{status: "disabled", reason: "raw message broadcast is intentionally disabled by policy"}`. There is no alternative canary that exercises the findings pipeline. Once the drift (DRIFT-001) is resolved and the default takes effect in prod, the pipeline has no end-to-end health signal | Add a `flow.canary_findings_check` that inserts a synthetic finding, awaits alert routing, and clears; keep the current canary but gate on the actual runtime policy so a wired-up canary is always available | M |
| LOGIC-002 | P1 | CONFIRMED | `app/workers/tasks/flow_tasks.py:hash_exfil_media:2970-2995` | Fetch filter `not_.is_("file_meta", "null")` includes empty-JSONB rows; every such row round-trips through `_download_media_bytes` and lands as a sentinel row. Wasted work + `media_hashes` bloat | Change to `.not_.eq("file_meta", "{}")` or a Python-side `if not fm.get("file_id"): continue` before download | S |
| LOGIC-003 | P2 | CONFIRMED | `app/workers/tasks/flow_tasks.py:broadcast_pending:990`, `settings.ENABLE_RAW_MESSAGE_BROADCAST` | Early-return `Disabled: raw message broadcast is opt-in; use the findings queue.` is silent from an alerting standpoint — beat continues to fire the task and every fire logs the disabled message. No metric or alert is emitted | Register a `metrics.inc("broadcast.disabled_run")` counter and expose it on `/health/metrics`; consider removing the beat entry when disabled | S |
| LOGIC-004 | P2 | CONFIRMED | `app/services/honeypot.py:151` (`app/api/routers/honeypot.py:151`) | `GET /honeypot/status` returns config booleans directly to the caller. Even with monitor-key gating, this leaks operational posture (`mode_enabled`, `secret_configured`, `allowlist_size`). If the monitor key ever leaks, this endpoint is a treasure map | Redact `secret_configured` to a checksum (`bool` only), drop `allowlist_size` when in AUTO mode, and gate this endpoint behind a stricter role than `require_monitor_key` (e.g. an operator-only key) | S |
| LOGIC-005 | P2 | CONFIRMED | `app/workers/tasks/flow_tasks.py:honeypot_redirect_one:4140` | `if not settings.HONEYPOT_REDIRECT_MODE: return {"status": "skipped", "reason": "not_authorized"}` — reason string is wrong (mode-off vs authorization-off). Confusing when triaging | Return `reason="mode_disabled"` or `"authorized_gate_off"` accurately | S |
| LOGIC-006 | P3 | CONFIRMED | `app/api/routers/ingest.py:130-160` | `_is_own_bot_token(token)` is called AFTER `security.encrypt(token)` and the `new_data` dict is built. Wasted CPU on ~kB of Fernet work when the token is rejected | Move the own-bot check to before encryption, right after `token_hash` calculation | S |
| LOGIC-007 | P3 | CONFIRMED | `app/services/audit.py:_TOKEN_RE = r'\b\d{5,15}[:%][A-Za-z0-9_-]{20,}\b'` | Regex allows 5-15 digit bot IDs and secret ≥ 20 chars; production tokens are 8-15/35. Overly permissive — will redact non-token strings that happen to match | Tighten to `\d{8,15}:[A-Za-z0-9_-]{35}\b` to match actual Telegram token shape | S |

### 8.6 PERF — Performance

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| PERF-001 | P2 | CONFIRMED | `audit_logs` = 357 357 rows, existing indexes `idx_audit_event_type` and `idx_audit_timestamp` are single-column | `/health/operational` queries filter on `event_type + timestamp`. Two single-column indexes force PG to bitmap-heap-scan. Without a composite `(event_type, timestamp DESC)`, sub-second responses degrade as the table grows | Add composite index `CREATE INDEX CONCURRENTLY idx_audit_event_type_timestamp ON audit_logs(event_type, timestamp DESC)`; combine with DATA-002 retention | S |
| PERF-002 | P2 | CONFIRMED | `app/workers/tasks/flow_tasks.py:_broadcast_logic:1055` | Serial per-message send with 1.5 s sleep. Batch 200 = ≥ 300 s per run. Single-thread throughput bottleneck for large backlogs | Use `asyncio.gather` over messages that target different topics (Telegram flood-wait is per-chat/topic, not global); keep sequential per-topic ordering | L |
| PERF-003 | P2 | POTENTIAL | `broadcaster_srv._download_media_bytes` (log evidence: connect+disconnect per photo, no pool) | Log shows Telethon `Connecting to 91.108.56.176:443/TcpFull...` + `Disconnection ...` per message. Missing MTProto client pooling for the broadcast side | Reuse a per-worker `TelegramClient` instance for media download; ensure disconnect only on worker shutdown | M |
| PERF-004 | P2 | CONFIRMED | `app/api/routers/monitor.py:_STATS_CACHE` | `/monitor/stats` cache is a per-process global (30 s TTL). With `gunicorn -w 4`, the cache is duplicated four times; stats fetches during miss-storm cost 4× | Move cache into Redis (`GET/SETEX monitor:stats`) so all workers share the miss cost | S |
| PERF-005 | P3 | POTENTIAL | `app/workers/tasks/validation_tasks.py:_refresh_pending_tokens_async` | 500 tokens re-enqueued with `asyncio.sleep(1)` every 50 → 10 s per run. Combined with `flow.produce_findings` and `flow.build_entity_graph` on the same worker, contention is possible. Redis token-bucket keeps external calls safe | Move refresh to the dedicated `validation` queue via `apply_async(queue='validation')` (already done in code review — verify it's the running behavior); leave as-is if OK | S |
| PERF-006 | P3 | POTENTIAL | `app/services/scanners.py:*` — 21 scanner classes, each `httpx.AsyncClient(timeout=10.0)` per call | Every scanner run creates fresh clients. No shared client pool | Introduce a per-service singleton `AsyncClient` (see `app/utils/http_client.get_async_http_client`) — the helper exists, use it consistently | M |

### 8.7 REL — Reliability

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| REL-001 | P0 | CONFIRMED | `docker inspect theprawnhunter_*` — all containers created `2026-08-28`; `git rev-parse HEAD = 3a033cf` (2026-09-05) | Running images are 8 days behind HEAD. Missing commits include `8986581 fix(api): rate limit bucket bypass via forged monitor keys`, `2350608 fix(security): is_dashboard_operator() guard`, `8dadb95 fix(security): honeypot redirect dual-gate`, `178cc49 fix(scripts): validate_deployment REST fallback`. `tasks.md` P0-005 documents this as the reason findings can be accessed without auth on Vercel | Rebuild and roll: `docker compose build api bot worker-core worker-scanners worker-scrape worker-validators beat flower frontend && docker compose up -d`; simultaneously push origin/main and trigger Vercel rebuild | M |
| REL-002 | P1 | CONFIRMED | `docker-compose.yml:frontend`, `docker-compose.yml:flower` | `docker inspect` on `theprawnhunter_flower` and `theprawnhunter_frontend` returns "map has no entry for key 'Health'" — no healthcheck defined. Silent failure risk; Compose will not restart a hung Flower or Next.js process | Add `healthcheck` blocks: Flower `curl -f http://localhost:5555 || exit 1`, frontend `wget --spider http://localhost:3000/` | S |
| REL-003 | P1 | CONFIRMED | Live log inspection (`docker logs --tail 500 theprawnhunter_worker-core` shows 101 broadcast events; audit_logs shows 731 `broadcast.failed` / 1 000 recent) | Broadcast failure rate is dominant. Root cause (per DATA-003): `media_archive_not_found` — the archival path fails to find media that the source bot no longer serves | Split into two counters: `broadcast.fail.transient` (retry-worthy) vs `broadcast.fail.permanent` (drop from queue after 5 attempts). Consider fetching + caching media at exfil-time rather than at broadcast-time | L |
| REL-004 | P2 | CONFIRMED | `docker-compose.yml:volumes` uses legacy prefix `telegramhunter_*` | Compose project name is `theprawnhunter` but volume prefix is `telegramhunter_`, tied to the pre-rename identity. External `redis_data`, `sessions`, `imports`, `beat_schedule` must be pre-created with that prefix; a fresh operator will hit `Error response from daemon: volume "telegramhunter_redis_data" not found` | Document the `docker volume create telegramhunter_*` prerequisite in README more prominently, or add a one-shot init script | S |
| REL-005 | P2 | CONFIRMED | `frontend/vite.config.ts` alongside a Next.js project | Vite config exists (used by Vitest) but there is no Vite build. Confusing to a new contributor | Rename to `vitest.config.ts` or `vite.config.test.ts` and document its role in `frontend/README.md` | S |
| REL-006 | P3 | POTENTIAL | `app/workers/celery_app.py` beat schedule uses `crontab(minute="*/5")` etc. | Beat schedule persists to `/app/beat/celerybeat-schedule` on a named volume; if the volume is corrupted or wiped, beat re-emits from time-of-restart. If the operator does `docker compose down -v` (destructive), the schedule persistence is lost. Documented in `docker-compose.yml` inline but easy to miss | Add a startup log line listing the schedule digest so operators can verify persistence | S |

### 8.8 FE — Frontend

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| FE-001 | P1 | CONFIRMED | `frontend/lib/supabase.ts`, `supabase/migrations/20260903000005_discovered_credentials_public_authenticated.sql` | Live Vercel and Docker `frontend` on old SHA — anonymous frontend visits will fail after redeploy because `discovered_credentials_public` view was moved from `anon` → `authenticated`. Overlaps SEC-003, REL-001; called out separately because it's a UX / policy regression path | After redeploy: verify `/signin` page shows and the findings view is empty until login; then confirm the RLS matches | S |
| FE-002 | P3 | CONFIRMED | `frontend/app/page.tsx` | Anon key is embedded in the client bundle at build time (`NEXT_PUBLIC_SUPABASE_KEY`). Expected for Supabase JS but should be paired with strict RLS on every table/view — currently the redacted-view GRANT to `authenticated` handles this correctly. Non-issue if RLS holds | Add a smoke test in CI that asserts `curl -H "apikey:$ANON" $SUPABASE_URL/rest/v1/discovered_credentials?limit=1` returns 401/403 | S |

### 8.9 FS — Filesystem

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| FS-001 | P3 | CONFIRMED | `.claude/scheduled_tasks.lock` | Claude Code hooks state file tracked in git — this is agent runtime state, not source | Add `.claude/*.lock` to `.gitignore`; remove from tracking with `git rm --cached .claude/scheduled_tasks.lock` | S |
| FS-002 | P3 | CONFIRMED | `.claude/settings.local.json`, `.playwright-mcp/page-2026-04-24T02-45-32-072Z.yml` | Local editor/tool artifacts tracked | Add `.playwright-mcp/`, `.claude/settings.local.json` to `.gitignore`; remove from tracking | S |
| FS-003 | P3 | CONFIRMED | root-level `plan.html`, `competitive-upgrade-plan.html`, `competitive-upgrade-plan-CORRECTED.html` | One-off HTML artifacts of an earlier planning pass. Not referenced by any code. Kept but not moved to `docs/history/` | Move to `docs/history/` (see §10) | S |



### 8.10 DRIFT — Documentation vs. Code

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| DRIFT-001 | P0 | CONFIRMED | `docker inspect` output; `tasks.md:P0-005`; `git log -n 30` | Deployment drift: running containers were built 2026-08-28, HEAD is 2026-09-05 with 8 days of security + reliability fixes unshipped | Rebuild + roll all 9 services and push the frontend to Vercel; verify by re-running `docker inspect | grep Created` after redeploy | M |
| DRIFT-002 | P2 | CONFIRMED | `app/core/config.py:Settings` HEAD has 79+ fields; running image has 75 (`ENABLE_RAW_MESSAGE_BROADCAST`, `HONEYPOT_REDIRECT_AUTHORIZED`, `PSEUDONYMIZATION_KEY`, `FINDING_ALERTS_ENABLED` missing) | Config-schema drift means new safety gates aren't enforced in prod. `ENABLE_RAW_MESSAGE_BROADCAST=False` intended default currently has no effect | Same as DRIFT-001 (redeploy); alternatively hotfix env if immediate rollback needed | S |
| DRIFT-003 | P2 | CONFIRMED | `PRD.md:22-40` says 7 services and concurrency 4/2/2; code has 10 services + concurrency 8/8/6/16 | Stale PRD misleads onboarding | Update PRD or delete it (see STRUCT-003) | S |
| DRIFT-004 | P2 | CONFIRMED | `PRD.md:26` says "25 tasks"; beat has ≈ 40 entries. `README.md:245` says "383 tests"; file count = 58 tests | Numbers in headline docs don't match code | Recount programmatically and update: `grep -c '"task":' app/workers/celery_app.py`, `pytest --collect-only -q | wc -l` | S |
| DRIFT-005 | P3 | CONFIRMED | `.env.template` still lists `SERPER_API_KEY`; `app/services/scanners.py:841` comment "Replaces SerperService"; no `SerperService` class exists | Dead env variable — operators may set it expecting a scanner | Remove `SERPER_API_KEY` from `.env.template` and `app/core/config.py`; delete the historical import references | S |
| DRIFT-006 | P3 | CONFIRMED | `README.md:66` says `CANARY_EXPECTED_TEXT` default `TheprawnHunter-canary`; `app/core/config.py:78` default is `telegramhunter-canary` | Case + hyphen mismatch confuses canary-message search | Update README to match code | S |
| DRIFT-007 | P3 | CONFIRMED | `SUPABASE_KEEPALIVE_SETUP.md:9` describes `keepalive_logs` (plural); code + `init.sql` use `keepalive_log` (singular) | Doc points operators at a table that doesn't exist | Rewrite `SUPABASE_KEEPALIVE_SETUP.md` against the actual schema (or delete — the workflow works correctly with `keepalive_log`) | S |
| DRIFT-008 | P3 | CONFIRMED | `AUDIT.md` (this file overwrites), `AUDIT_LOG.md`, `security_audit.md` (2026-05-24) | Prior audits describe a scan of `.venv-test/*` (3 680 files, N/A production) — misrepresents the codebase | Archive under `docs/history/2026-05-audits/` after this run overwrites `AUDIT.md` | S |

### 8.11 DEAD — Dead Weight

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| DEAD-001 | P3 | CONFIRMED | `.env.template` `SERPER_API_KEY=`, `app/core/config.py` `SERPER_API_KEY: str \| None = None` | `SerperService` deleted; env variable and Settings field remain | Delete field and template line; grep for stray imports | S |
| DEAD-002 | P3 | CONFIRMED | `app/core/config.py:CENSYS_ID, CENSYS_SECRET, HYBRID_ANALYSIS_KEY` | Fields declared, no scanner class or task references them; `POST /scan/trigger` already blocks `censys` / `hybrid` (`app/api/routers/scan.py:31`) | Delete fields | S |
| DEAD-003 | P3 | CONFIRMED | root `AUDIT.md` (about to be overwritten), `AUDIT_LOG.md`, `security_audit.md` | 2026-05-24 audit artifacts, obsolete | Move to `docs/history/` and delete from root | S |
| DEAD-004 | P3 | CONFIRMED | root `bugfix.md`, `design.md`, `tasks.md` | Historical work-tracking files with completed and abandoned items intermixed. `tasks.md` P0-005 is still relevant (covered by DRIFT-001) | Move to `docs/history/`, keep `tasks.md` as an active runbook (or replace with GitHub Issues) | M |
| DEAD-005 | P3 | CONFIRMED | `.deepsource.toml`, `.sourcery.yml` | DeepSource and Sourcery config present; no evidence either is running (no badges, no PR comments in `gh pr list --state merged --limit 30`) | Confirm with the tooling dashboards; delete if inactive | S |
| DEAD-006 | P3 | CONFIRMED | `.playwright-mcp/page-2026-04-24T02-45-32-072Z.yml` | Recorded browser session, one-off | Delete + `.gitignore` `.playwright-mcp/` | S |
| DEAD-007 | P3 | CONFIRMED | `app/services/scanners.py` — `retry_with_backoff` helper defined at top; only referenced internally within `scanners.py` classes | Not exported for tests, not imported elsewhere. Fine as-is but a doc-comment would help | Move to `app/utils/http_client.py` for reuse | S |
| DEAD-008 | P2 | CONFIRMED | 142 `except Exception:` / bare `except:` occurrences across `app/**/*.py` | Broad exception swallowing on this scale hides real failures. Some are legitimate (best-effort telemetry), many aren't | Grep for `pass` inside the `except`, review each; consider a lint rule to require a `logger.exception` inside broad excepts | L |

### 8.12 STRUCT — Structure & Organization

| ID | SEV | CONF | FILE:LINE | ISSUE | FIX | EFFORT |
|---|---|---|---|---|---|---|
| STRUCT-001 | P2 | CONFIRMED | root: `AUDIT.md`, `AUDIT_LOG.md`, `PRD.md`, `design.md`, `tasks.md`, `bugfix.md`, `security_audit.md`, `plan.html`, `competitive-upgrade-plan*.html`, `SUPABASE_KEEPALIVE_SETUP.md` | 10 documentation / planning artifacts at root — clutters the tree and blurs canonical vs. historical | Move to `docs/history/` (see §10). Keep `README.md`, `LICENSE`, `NOTICE`, `AUDIT.md` (this file) at root | S |
| STRUCT-002 | P2 | CONFIRMED | `.env.template` at root; `.gitignore` whitelists `!.env.example` but no file matches | Whitelist entry references a non-existent file | Remove the `!.env.example` line, or rename `.env.template` → `.env.example` for convention alignment | S |
| STRUCT-003 | P3 | CONFIRMED | `.claude/`, `.kiro/`, `.agents/`, `.playwright-mcp/` co-exist at root | Multiple AI-tool state directories; some tracked, some gitignored | Consolidate under `.agents/` (already partly gitignored) or explicitly document each in `README.md` | S |
| STRUCT-004 | P3 | CONFIRMED | `frontend/vite.config.ts` alongside Next.js — see REL-005 | Same as REL-005 | Rename config file to reflect its Vitest-only role | S |
| STRUCT-005 | P3 | CONFIRMED | `app/services/_scraper/` (private-underscore prefix) vs `app/services/*.py` | Private-package convention only appears here; other private modules use no underscore | Either normalise all internal-only submodules with `_` prefix or drop the prefix here | S |
| STRUCT-006 | P3 | CONFIRMED | `app/workers/tasks/_scanner/` — same private-prefix convention | Consistent with `_scraper/` — noted as intentional. Not a bug | No action | S |
| STRUCT-007 | P3 | CONFIRMED | `database/migrations/` and `supabase/migrations/` coexist | Two migration directories — `database/migrations/` holds pre-Supabase-CLI patches, `supabase/migrations/` holds the current versioned migrations | Consolidate under `supabase/migrations/`; archive `database/migrations/` under `docs/history/` | M |

---

## 9. Interruption & Recovery Analysis

Every stateful path in the system was traced against interruption sources listed in the prompt (kill -9, OOM, pod eviction, network partition, DB failover, cache outage, browser close, forced deploy).

| Path / operation | Interruption point | Consequence | Recoverable? | Finding |
|---|---|---|---|---|
| Scanner → `_save_credentials_async` enqueue | Between scanner task success and `validate_token.delay(item, source_name)` per token | Some tokens for that scanner run are not queued; next scan run re-discovers them | Yes — next scanner run, plus Redis `validated:recent:*` dedup skip | — |
| `validation.validate_token` — Telegram getMe → DB insert | Between successful getMe and DB insert | Token was validated once but no DB row exists; next validator run repeats the getMe (Redis dedup skips soft-recent tokens for 24 h — burns a token) | Yes | INTR-004 |
| `flow.enrich_credential` — Telethon `get_dialogs` → topic creation → meta write | Between topic creation on Telegram side and `meta.topic_id` DB write | Topic exists in the monitor group but DB `meta` still lacks `topic_id`. Next broadcast picks the wrong path (`ensure_topic` creates a duplicate topic) | Partial — `flow.reconcile_topics_from_db` (hourly) writes `topic_id` after resolving; still a race | — (covered by `reconcile_topics_from_db`) |
| `flow.exfiltrate_chat` — self-heal encrypt | Between successful send (unchanged in this path — no send yet) and DB write for the encrypted token | Raw token remains in DB and is used in memory for downstream; if downstream fails and the row is later exported, plaintext leaks | Yes if next exfil retries the self-heal | INTR-004 |
| `flow.exfiltrate_chat` — Telethon scrape → message upsert | Kill mid-loop over `messages` | Some messages inserted, others not; next `flow.rescrape_active` picks up the rest via anchor logic | Yes | — |
| `flow.broadcast_pending` — send → `is_broadcasted=true` update | Kill after Telegram accepts message, before DB write | Message duplicated on next claim | Yes but with duplicate broadcast | **INTR-001** |
| `flow.honeypot_redirect_one` — send → `redirected_at` update | Kill between Telegram send and DB / Redis dedup mark | Redirect resent on next sweep | Yes with duplicate DM to user | **INTR-002** |
| `system.import_csv` — rename `.csv → .pending`, parse, `_save_credentials_async`, move to `processed/` | Kill mid-parse; startup renames `.pending → .csv` unconditionally | Partial re-enqueue on next run; Redis `validated:recent:*` protects downstream | Yes | **INTR-003** |
| `flow.hash_exfil_media` — download → hash → INSERT into `media_hashes` | Kill between download and INSERT | Next run refetches; needs `UNIQUE(message_id)` on `media_hashes` to avoid dup rows | Partial | **CONC-003, DATA-004, INTR-005** |
| `bot_listener` `/starthunter` — code → password → session file | Kill during any of 3 conversation states | Session file left in `/tmp` — swept on next `main()` startup (`app/services/bot_listener.py:1544`) and on `ConversationHandler.TIMEOUT` | Yes | — |
| `bot_listener` — poll lock renewal (`LOCK_TTL_SECONDS=120`) | Kill between renewals | Lock expires ≤ 120 s; another bot instance takes over. `docker-entrypoint.sh` clears stale `telegram_accounts` leases (only expired ones) | Yes | — |
| Redis rate-limit token (`rate_limit:telegram_getMe`) | Redis restart | Bucket reset to 0; brief validation burst possible in first 10 s | Yes | — |
| Beat schedule persistence | `docker compose down -v` destroys `telegramhunter_beat_schedule` volume | Beat replays from scratch — no immediate harm because tasks are idempotent | Yes | REL-006 |
| API gunicorn worker crash | uvicorn worker restart | New process picks up next request. Rate-limit and stats caches per-worker warm-up cost | Yes | PERF-004 |
| DB row lease `broadcast_claimed_at` | Worker kills after claim | 15-min timeout releases the claim; another worker retries. Risk of double-broadcast (INTR-001) | Partial | INTR-001 |
| `worker-scrape` OOM during Telethon `iter_messages` | Kill after some messages processed | Upsert-with-`ignore_duplicates` guarantees no duplicates; next `rescrape_active` completes | Yes | — |
| Telegram MTProto session file corruption | Filesystem loss / partial write | `AuthKeyUnregistered` on next connect; scraper classifies as permanent and revokes the credential | Yes, but real session may be lost | — |
| `docker compose down -v` | Full volume wipe | `redis_data`, `sessions`, `imports`, `beat_schedule` volumes destroyed. Sessions are the only unrecoverable loss | No (session loss) | REL-004 documents |
| Vercel deploy interrupted mid-build | Vercel rollback | Frontend continues on last successful build | Yes | — |
| Cloudflare Tunnel dies (honeypot) | Telegram `setWebhook` still points to public URL; POSTs 5xx | Telegram retries with backoff, but the pending update budget can fill. On tunnel recovery, backlog delivers | Partial | — |
| audit_logs INSERT during Supabase failover | `AuditLogger._persist_to_db` fails, catches exception, throttles missing-table log | Audit event is lost from DB but retained in stdout via `logger` | Partial | DATA-002 |

Interruption findings register: **INTR-001** (P1) … **INTR-005** (P3).

---

## 10. Structural Reorganization Plan

### 10a. Current file tree (top-level + one level, elided depth 2+)

```
theprawnhunter/
├── .agents/diagnosis/               6 diagnosis notes (2026-08-28 → 2026-09-05)
├── .claude/                         scheduled_tasks.lock (tracked, see FS-001) + settings.local.json
├── .github/                         dependabot, funding, 7 workflows
├── .kiro/specs/scanner-query-coverage/    kiro spec artifacts
├── .playwright-mcp/                 recorded browser transcript (see FS-002)
├── app/
│   ├── api/{main.py, routers/*}     6 routers
│   ├── core/                        16 modules
│   ├── schemas/models.py
│   ├── services/                    14 modules + _scraper/ (5)
│   ├── utils/
│   └── workers/{celery_app.py, flower_app.py, tasks/}
├── database/
│   ├── init.sql
│   ├── rls_policies.sql
│   ├── operations/retention_cleanup.sql
│   └── migrations/*.sql             8 patch migrations (pre-supabase-cli)
├── docs/
│   ├── HONEYPOT.md
│   ├── cloudflare_waf_rules.md
│   ├── production_runbook.md
│   ├── telegram_probe_matrix.example.json
│   └── plans/                       3 plan docs
├── extension/                       Manifest V3 FOFA scraper
├── frontend/                        Next.js 16 dashboard + Vitest
├── imports/README.md
├── scripts/                         15 operational scripts
├── supabase/
│   ├── config.toml
│   └── migrations/*.sql             21 dated migrations
├── tests/                           59 test files
├── AUDIT.md                         *this file*
├── AUDIT_LOG.md                     obsolete 2026-05
├── PRD.md                           stale 2026-04
├── README.md                        current
├── SUPABASE_KEEPALIVE_SETUP.md      stale (see DRIFT-007)
├── bugfix.md                        historical
├── competitive-upgrade-plan-CORRECTED.html    one-off
├── competitive-upgrade-plan.html    one-off
├── design.md                        historical
├── docker-compose.yml
├── docker-compose.prod.yml
├── docker-entrypoint.sh
├── Dockerfile
├── LICENSE
├── NOTICE
├── package-lock.json / package.json
├── plan.html                        one-off
├── pyproject.toml
├── requirements.txt / requirements-dev.txt
├── security_audit.md                obsolete 2026-05
├── start.bat / start.sh
├── tasks.md                         still relevant (P0-005 open)
└── REPO_MAP.md                      produced by 00_MAP (currently untracked)
```

### 10b. Target file tree (proposed)

```
theprawnhunter/
├── .agents/                         (unchanged; add .gitignore entry for local subdirs)
├── .github/                         (unchanged)
├── app/                             (unchanged — layout is correct)
├── database/
│   ├── init.sql
│   ├── rls_policies.sql
│   └── operations/retention_cleanup.sql
├── docs/
│   ├── HONEYPOT.md
│   ├── PRD.md                       (moved from root; refreshed to match code)
│   ├── cloudflare_waf_rules.md
│   ├── production_runbook.md
│   ├── SUPABASE_KEEPALIVE.md        (moved + renamed + rewritten against `keepalive_log`)
│   ├── plans/                       (unchanged)
│   └── history/                     (new — see 10d)
│       ├── 2026-05-audits/
│       │   ├── AUDIT_LOG.md
│       │   └── security_audit.md
│       ├── bugfix.md
│       ├── design.md
│       ├── legacy_migrations/*.sql  (moved from database/migrations/)
│       ├── plan.html
│       └── competitive-upgrade-plan{,-CORRECTED}.html
├── extension/                       (unchanged)
├── frontend/
│   ├── ...                          (unchanged)
│   └── vitest.config.ts             (renamed from vite.config.ts)
├── imports/README.md
├── scripts/                         (unchanged)
├── supabase/
│   ├── config.toml
│   └── migrations/*.sql             (unchanged; DB-only tests still here)
├── tests/                           (unchanged)
├── AUDIT.md                         (current audit, this file)
├── README.md
├── tasks.md                         (kept — active operational backlog)
├── docker-compose.yml / .prod.yml
├── docker-entrypoint.sh
├── Dockerfile
├── LICENSE / NOTICE
├── package.json / package-lock.json
├── pyproject.toml
├── requirements.txt / requirements-dev.txt
└── start.bat / start.sh
```

Purpose annotations for new directories:

- `docs/history/` — frozen archive of prior audits, plans, and historical bug ledgers. Read-only for maintainers, useful for provenance, kept out of the root tree.
- `docs/history/2026-05-audits/` — the 2026-05-24 audit trio (`AUDIT_LOG.md`, `security_audit.md`, plus the pre-overwrite `AUDIT.md` — see backup step below).
- `docs/history/legacy_migrations/` — the 8 pre-`supabase/`-CLI patch migrations in `database/migrations/`, retained for schema-history reference. Never re-executed.

### 10c. Move plan (sequenced)

| Step | Action | Source | Destination | Protected? | Backup required? |
|---|---|---|---|---|---|
| 1 | Backup pre-existing `AUDIT.md` (2026-05 version — this run overwrote it) | `AUDIT.md` (git blob at `HEAD~1` if needed) | `docs/history/2026-05-audits/AUDIT.md` | No | Yes — recover via `git show HEAD:AUDIT.md > docs/history/2026-05-audits/AUDIT.md` before overwriting |
| 2 | Move `AUDIT_LOG.md` | root | `docs/history/2026-05-audits/AUDIT_LOG.md` | No | No |
| 3 | Move `security_audit.md` | root | `docs/history/2026-05-audits/security_audit.md` | No | No |
| 4 | Move `bugfix.md` | root | `docs/history/bugfix.md` | No | No |
| 5 | Move `design.md` | root | `docs/history/design.md` | No | No |
| 6 | Move `plan.html` | root | `docs/history/plan.html` | No | No |
| 7 | Move `competitive-upgrade-plan.html` | root | `docs/history/competitive-upgrade-plan.html` | No | No |
| 8 | Move `competitive-upgrade-plan-CORRECTED.html` | root | `docs/history/competitive-upgrade-plan-CORRECTED.html` | No | No |
| 9 | Refresh `PRD.md` against current code, then move | root | `docs/PRD.md` | No | No |
| 10 | Rewrite `SUPABASE_KEEPALIVE_SETUP.md` against `keepalive_log` singular table, then move | root | `docs/SUPABASE_KEEPALIVE.md` | No | No |
| 11 | Move legacy patch migrations | `database/migrations/*.sql` (8 files) | `docs/history/legacy_migrations/` | **YES** (migration files) | Yes — protected-file rule; create `docs/history/legacy_migrations/` first, then `git mv` each with SHA verification; do NOT delete |
| 12 | Delete `database/migrations/README.md` after step 11 (empty directory) | `database/migrations/` | — | No | No |
| 13 | Rename `frontend/vite.config.ts` → `frontend/vitest.config.ts`; update `frontend/package.json` test script if needed | `frontend/vite.config.ts` | `frontend/vitest.config.ts` | No | No |
| 14 | `git rm --cached .claude/scheduled_tasks.lock` + `.claude/settings.local.json`; add both to `.gitignore` | — | — | No | No — but keep working-copy files |
| 15 | `git rm --cached .playwright-mcp/*.yml`; add `.playwright-mcp/` to `.gitignore` | — | — | No | No |

Every move is executed with `git mv` (preserves history). No file is deleted; the migration files (step 11) are relocated only, per the protection rule. The `.env`, `.env.template`, `*.session`, `*.key`, `*.pem` categories are untouched by this plan.

### 10d. New directories

| Directory | Purpose |
|---|---|
| `docs/history/` | Frozen historical artifacts (audits, plans, legacy design docs). Read-only. |
| `docs/history/2026-05-audits/` | Prior audit trio, kept for provenance. |
| `docs/history/legacy_migrations/` | Pre-supabase-CLI patch migrations, retained for schema-history reference; never re-executed. |

### 10e. `.gitignore` additions

| Pattern | Reason |
|---|---|
| `.claude/*.lock` | Claude Code hooks state (FS-001) |
| `.claude/settings.local.json` | Per-machine editor settings (FS-002) |
| `.playwright-mcp/` | Recorded browser sessions (FS-002, DEAD-006) |
| `!.env.example` (existing whitelist) | Remove — no such file (STRUCT-002) |

---

## 11. Production Readiness Checklist

| # | Item | Status | Justification | Covers |
|---|---|---|---|---|
| 1 | All secrets externalized to environment variables — none hardcoded | **PASS** | No hardcoded credentials found in tracked files (`git grep` for `ghp_`, `AIza`, `-----BEGIN` returned empty). `EXTENSION_WRITE_SECRET` lives only in the Supabase DB parameter. | — |
| 2 | Dependencies pinned to explicit versions with no known CVEs | **PARTIAL** | `requirements.txt` pins exact versions (cryptography 46.0.7, fastapi 0.136.0 with inline CVE citations). Dev dep `pytest==8.3.5` note: "CVE-2025-71176 — no patched version exists yet; kept out of prod image". `requirements-dev.txt` is not in the prod image, so acceptable. Bandit + Semgrep + TruffleHog CI workflows run weekly. | — |
| 3 | Database migrations versioned and reversible | **PARTIAL** | 21 dated migrations in `supabase/migrations/` are forward-only. Rollback path is documented only via manual DDL. `database/migrations/` (8 patch files) are pre-supabase-cli era. **Four migrations are unapplied on live Supabase** (DATA-001). | DATA-001, STRUCT-007 |
| 4 | All external API calls have timeout AND retry configuration | **PASS** | Every `httpx.AsyncClient` sets `timeout=`. `@retry` decorator and `retry_with_backoff` used across scanners. Circuit breakers on `shodan`, `urlscan`, `github`, `fofa`. | — |
| 5 | Logging is structured (JSON or key-value), not ad-hoc prints | **PASS** | `stdlib logging` with JSON-friendly formatter; Celery worker log lines are JSON-ish. Container-side json-file driver rotates 10 MB × 3. | — |
| 6 | No debug routes, test endpoints, or dev-only flags on production paths | **PASS** | `POST /scan/trigger` returns 403 when `ENV=production`; `/docs`, `/redoc`, `/openapi.json` return 404. `ALLOW_PUBLIC_STARTHUNTER` defaults False. | — |
| 7 | Graceful shutdown handling for every long-running process | **PASS** | Celery `worker_shutdown` signal closes the persistent event loop and sends a Telegram notice. `bot_listener` sets a stop_event on SIGINT/SIGTERM (POSIX). FastAPI lifespan schedules a daemon-thread shutdown log. | — |
| 8 | Error responses leak no stack traces or internal paths | **PASS** | Routers catch top-level `Exception`, log via `logger.exception`, respond with generic `HTTPException(500, "Internal error")`. `/scan/trigger` explicitly avoids echoing exception text (may contain broker DSN). `/media/{id}` scrubs Telegram-token URLs from responses. | — |
| 9 | Input validation at every external-facing interface | **PASS** | Pydantic models on all request bodies (`ScanRequest`, `ExtensionIngestRequest`, `EngagementLifecycleIn`, `FindingFeedbackIn`). `/honeypot/receive` validates JSON structure and `update_id`. Extension write path additionally gated via `x-extension-secret` in RLS. | — |
| 10 | Health check endpoint or equivalent monitoring hook present | **PARTIAL** | `/health/` (200 open), `/health/detailed`, `/health/queues`, `/health/circuit-breakers` all wired. Container-level: `api`, `bot`, workers, `beat`, `redis` have healthchecks. `flower` and `frontend` do not. | REL-002 |
| 11 | All file writes atomic or guarded against partial-write corruption | **PARTIAL** | `system.import_csv` uses atomic rename claim. Session files: Telethon handles atomic-ish via SQLite. `celerybeat-schedule` uses a `.db` file with lockfile. **Broadcast → is_broadcasted is not atomic** (INTR-001). | INTR-001 |
| 12 | Rate limiting or abuse prevention on public endpoints | **PASS** | `slowapi` 120 req/min per key/IP, Redis-backed. `_rate_key` uses constant-time compare on the monitor key. `/honeypot/receive/{id}` fail-closed if `HONEYPOT_MODE=False`. | — |
| 13 | All auth tokens and sessions have expiry logic | **PARTIAL** | `MONITOR_API_KEY` has no expiry — rotate manually. `telegram_accounts.locked_until` expires after 10 min. Extension `x-extension-secret` has no rotation surface. | — (accepted design) |
| 14 | Test coverage exists for every critical path, even if minimal | **PARTIAL** | 46 unit + 3 integration + 1 load + 8 top-level = 58 test files. Tests exist for scanners, findings, engagement, RLS auth, broadcast retry, media forensics, telemetry indexing, honeypot dual-gate, dashboard operator authorization. **Not covered**: `bot_manager_srv.py`, `pivot_tasks.py`, `firehose_tasks.py`, `import_tasks.py`, `extension/*`, most scripts. | DATA-002 (blast radius) |
| 15 | Build/start process documented and reproducible | **PASS** | `README.md` install section explicit; `Dockerfile` multi-stage; volume creation prerequisite documented inline in `docker-compose.yml`. | REL-004 documents legacy prefix |
| 16 | Every write path that can be retried is idempotent | **FAIL** | Broadcast (INTR-001), honeypot redirect (INTR-002), and self-heal encrypt (INTR-004) all have write windows where retry duplicates the action. `_index_telemetry_indicators` uses UPSERT (idempotent). Message insertion uses UPSERT `on_conflict='credential_id,telegram_msg_id' ignore_duplicates=True` (idempotent). | INTR-001, INTR-002, INTR-004 |
| 17 | Every background job survives being killed mid-execution | **FAIL** | Broadcast, honeypot redirect, and media hash sentinel paths have partial-completion states that either duplicate work or need manual reconciliation. `import_csv` recovers `.pending → .csv` correctly. | INTR-001, INTR-002, INTR-003, INTR-005 |

**Score: 8 PASS · 5 PARTIAL · 2 FAIL · 0 N/A (of 17)**.

---

## 12. Prioritized Remediation Roadmap

Execution order strictly follows the prompt-defined sequence.

| Order | Finding ID | Action | Rationale | Files affected | Effort |
|---|---|---|---|---|---|
| 1 | REL-001 / DRIFT-001 | Rebuild and roll all 9 backend containers to HEAD; push origin/main and trigger Vercel redeploy | Missing 8 days of security fixes (rate-limit bypass, RLS operator guard, honeypot dual-gate) are the reason SEC-003 and FE-001 are still exploitable | `docker-compose.yml`, all images, Vercel project | M |
| 2 | SEC-001 | Encrypt the 478 plaintext bot tokens in place; then remove anon `INSERT`/`UPDATE` policies from `discovered_credentials`; force extension writes through `/ingest/extension/credentials` | 47.8 % of sampled rows are plaintext bot tokens. RLS bypass or leaked service-role key exposes 478 live credentials | `database/rls_policies.sql`, `extension/background.js`, one-off migration script | L |
| 3 | DATA-001 | Apply the four missing migrations to Supabase | `/monitor/findings/*`, `flow.produce_findings`, `flow.build_entity_graph`, honeypot multi-touch tasks are all failing silently | Live Supabase (via SQL editor) | S |
| 4 | SEC-002 | Replace inline compare on `/honeypot/status` with `require_monitor_key` dependency | Constant-time consistency; hardening | `app/api/routers/honeypot.py:151-160` | S |
| 5 | SEC-003 | Confirm frontend redeploy fully rolled out (FE-001 same fix), smoke-test anon `SELECT` returns 401/403 | Verifies remediation of REL-001 for the customer-facing surface | Vercel + frontend Docker image | S |
| 6 | INTR-001 | Idempotent broadcast via recorded outgoing Telegram message-id | Eliminates duplicate broadcast on worker interruption | `app/workers/tasks/flow_tasks.py`, add column `broadcast_message_id BIGINT` on `exfiltrated_messages` | M |
| 7 | INTR-002 | Claim-before-send for honeypot redirect (`redirected_at='pending'`) | Eliminates duplicate DM to victim | `app/workers/tasks/flow_tasks.py:honeypot_redirect_one` | M |
| 8 | INTR-003 | Persist `.done` breadcrumb per CSV; skip recovery if breadcrumb exists | Avoids re-enqueuing already-imported CSV rows | `app/workers/tasks/import_tasks.py` | S |
| 9 | INTR-004 | Encrypt-then-persist before any downstream use; raise on persist failure | Closes the self-heal window (overlaps SEC-001) | `app/workers/tasks/flow_tasks.py:_exfiltrate_logic` | S |
| 10 | STRUCT-001..007 | Execute the move plan in §10c using `git mv` (protection-rule enforced) | Cleans repo root; consolidates migrations; renames Vitest config | See §10c | S–M |
| 11 | DATA-002 | Reduce audit `_should_persist` scope; add `(event_type, timestamp)` composite index (PERF-001) | Table bloat + slow `/health/operational` | `app/core/audit.py`, `database/init.sql` | M |
| 12 | DATA-003 | Cap `broadcast_attempts` at 8 → permanent-failed; add operator clear path | Frees the 1 478 stuck messages | `app/workers/tasks/flow_tasks.py`, monitor router | M |
| 13 | REL-002 | Add healthchecks to `flower` and `frontend` services | Docker Compose can restart hung processes | `docker-compose.yml` | S |
| 14 | REL-003 | Split broadcast failure metrics; consider exfil-time media caching | Recover broadcast success rate | `broadcaster_srv.py`, `flow_tasks.py` | L |
| 15 | LOGIC-001 | Add `flow.canary_findings_check` end-to-end canary | End-to-end health signal survives the raw-broadcast toggle | new file in `app/workers/tasks/` | M |
| 16 | LOGIC-002 | Filter empty-JSONB `file_meta` before download attempt | Prevents sentinel-row growth in `media_hashes` | `app/workers/tasks/flow_tasks.py:hash_exfil_media` | S |
| 17 | CONC-002 / CONC-003 / INTR-005 | Add `UNIQUE(message_id)` on `media_hashes`; claim-before-dispatch on honeypot sweep; replace failure sentinel with `is_failure BOOLEAN` | Race-safety and schema clarity | migration + `flow_tasks.py` | M |
| 18 | PERF-001 | Composite `(event_type, timestamp DESC)` index on `audit_logs` (paired with DATA-002) | Query performance | migration | S |
| 19 | PERF-002 / PERF-003 | Parallelise broadcast across topics; pool MTProto media-download client | Broadcast throughput | `broadcaster_srv.py`, `flow_tasks.py` | L |
| 20 | PERF-004 | Move `/monitor/stats` cache to Redis | Cross-worker cache sharing | `app/api/routers/monitor.py` | S |
| 21 | DRIFT-002..008 | Update or archive doc drift entries (PRD/README numbers, canary text, keepalive doc, obsolete audits) | Aligns docs with code | See §6 rows | M |
| 22 | LOGIC-003..007, SEC-004..006 | P2/P3 quality patches: metrics for disabled broadcasts, redact `/honeypot/status`, correct reason strings, order own-bot check before encrypt, tighten audit token regex, TLS-opt-in for webhook probes | Hardening + hygiene | Various | S each |
| 23 | DRIFT-005, DEAD-001..008, FS-001..003 | Remove dead env vars (`SERPER_API_KEY`, `CENSYS_*`, `HYBRID_ANALYSIS_KEY`); untrack `.claude/*.lock`, `.playwright-mcp/`; audit 142 broad-except sites | Codebase cleanup | `.env.template`, `config.py`, `.gitignore` | S–L |

---

## 13. Selection Prompt

Audit complete. **69 findings** across 12 categories (SEC 6 · DATA 7 · CONC 4 · INTR 5 · LOGIC 7 · PERF 6 · REL 6 · FE 2 · FS 3 · DRIFT 8 · DEAD 8 · STRUCT 7). Severity split: P0 × 4 · P1 × 11 · P2 × 26 · P3 × 28.

**Nothing has been fixed. No source file has been modified in this stage.** The only file written is this `AUDIT.md`.

The four P0 items are:

- **DRIFT-001 / REL-001** — Running Docker images are 8 days behind HEAD; recent security fixes (rate-limit bypass, RLS operator guard, honeypot dual-gate, frontend RLS hardening) are not deployed.
- **SEC-001** — 478 (47.8 %) of sampled `discovered_credentials` rows store plaintext bot tokens.
- **DATA-001** — Four migrations are unapplied on live Supabase (`findings`, `finding_evidence`, `engagement_events`, `honeypot_redirect_log`); shipping code depends on them.
- **DRIFT-001** (repeated because it is both a REL and DRIFT concern) — same underlying cause as REL-001.

Which do you want fixed? Answer one of:

- `fix all`
- `fix P0` or `fix P0,P1`
- Specific IDs, e.g. `fix SEC-001, DATA-001, INTR-001, REL-001`
- `fix all except STRUCT-*`
- `fix P0 and P1 only, defer P2 and P3`
- `none — report only`

Selected IDs will be handed off to `02_EXECUTE` alongside this AUDIT.md.
