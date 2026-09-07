# Repository Map

## 1. Provenance
- Repository name / remote: `origin` → GitHub-hosted repository (see `git remote -v`); second remote `upstream` → `github.com/0x6rss/matkap.git` (the original fork ancestor).
- Commit analyzed: `3a033cfe06123627c37cbfb03662a9a543c61ebf`.
- Branch: `main`.
- Working tree: 0 modified, 0 untracked (`git status --porcelain` returned no lines).
- PR data source: `gh` (authenticated).
- Agent capability: shell execution + arbitrary file reads.
- Not analyzed: contents of binary assets (`.png`, `.svg`); frontend `package-lock.json` (9 092 lines, lockfile); `.playwright-mcp/page-2026-04-24T02-45-32-072Z.yml` (recorded browser transcript); the four HTML report artifacts (`plan.html`, `competitive-upgrade-plan.html`, `competitive-upgrade-plan-CORRECTED.html`, `frontend/app/icon.png`); no submodules or vendored directories are present.

## 2. Provenance-derived description
The repository holds a self-hosted OSINT stack that continually queries public data providers for Telegram Bot API tokens, verifies each candidate through the `getMe` HTTPS endpoint (`app/workers/tasks/validation_tasks.py`), scrapes accessible chat history through Telethon MTProto sessions (`app/services/scraper_srv.py`), and delivers findings into a private supergroup organised into per-credential forum topics (`app/services/broadcaster_srv.py`). Discovered tokens, message bodies, media metadata and audit events are persisted to a managed PostgreSQL instance addressed through the Supabase Python client (`app/core/database.py`, `database/init.sql`). Ten Docker Compose services back the stack — a Redis broker, a FastAPI HTTP layer, four Celery worker queues, a Celery beat scheduler, a python-telegram-bot admin listener, a Flower monitor, and a Next.js dashboard (`docker-compose.yml`). A Manifest V3 Chrome extension (`extension/manifest.json`) provides an out-of-band ingestion path that either POSTs to `/ingest/extension/credentials` or writes directly to Supabase under an extension-secret RLS policy.

## 3. Quick facts
| Field | Value |
|---|---|
| Primary language(s) | Python (136 tracked `.py`), TypeScript/TSX (17 `.ts`/`.tsx`), SQL (32), YAML (15), JavaScript (3 `.js` + 3 `.mjs`) |
| Runtime / version constraint | Python 3.11 (`Dockerfile:5`, `pyproject.toml:6`); Node 20 for the frontend build stage (`frontend/Dockerfile:1`); Node 22 in CI (`.github/workflows/ci.yml:73`) |
| Package manager | `pip` + `requirements.txt` / `requirements-dev.txt`; frontend uses `npm` with `package-lock.json` |
| Tracked files | 286 |
| Total lines (tracked) | 57 433 |
| Deployment artifact | Docker Compose stack (10 services) built from `Dockerfile` + `frontend/Dockerfile` (`docker-compose.yml`, `docker-compose.prod.yml`) |
| Persistence | Supabase-hosted PostgreSQL accessed via `supabase==2.28.3` client (`app/core/database.py`); schema in `database/init.sql`; 21 dated migrations under `supabase/migrations/`; 8 patch migrations under `database/migrations/` |
| Test framework(s) | `pytest` 8.3.5 + `pytest-asyncio` 0.24 for Python (`pyproject.toml` `[tool.pytest.ini_options]`); `vitest` 2 + Testing Library for the frontend (`frontend/package.json`) |
| CI | GitHub Actions: `.github/workflows/ci.yml`, `.github/workflows/bandit.yml`, `.github/workflows/semgrep.yml`, `.github/workflows/trufflehog.yml`, `.github/workflows/supabase-keep-alive.yml`, `.github/workflows/labeler.yml`, `.github/workflows/greetings.yml` |
| License | Apache-2.0 (`LICENSE:1`, `NOTICE`) |

## 4. How it runs

| Entry point | Path | Trigger | What it starts |
|---|---|---|---|
| FastAPI ASGI app | `app/api/main.py:94` | `gunicorn ... uvicorn.workers.UvicornWorker` in `docker-compose.yml` (`api` service) | HTTP API on container port 8001, host `${API_PORT:-8011}` |
| Celery core worker | `app/workers/celery_app.py:32` | `celery -A app.workers.celery_app worker -Q celery --concurrency=8` (`worker-core`) | Runs `flow.*`, `audit.*`, `system.*` tasks |
| Celery scanners worker | same module | `-Q scanners --concurrency=8` (`worker-scanners`) | Runs `scanner.*` and `firehose.*` tasks |
| Celery scrape worker | same module | `-Q scrape --concurrency=6` (`worker-scrape`) | Runs `flow.exfiltrate_chat`, `flow.rescrape_active` |
| Celery validation worker | same module | `-Q validation --concurrency=16` (`worker-validators`) | Runs `validation.*`, `pivot.*` tasks |
| Celery beat | same module | `celery beat --schedule /app/beat/celerybeat-schedule` | Enqueues ~40 scheduled entries (`beat_schedule=` block, `app/workers/celery_app.py:220-500`) |
| Telegram bot listener | `app/services/bot_listener.py:1601` (`if __name__ == "__main__"`) → `main()` at line 1544 | `python -m app.services.bot_listener` (`bot` service) | Multi-bot polling loop with admin command handlers |
| Flower monitor | `app/workers/flower_app.py:14` | `celery -A app.workers.flower_app flower --port=5555 --basic_auth=$FLOWER_BASIC_AUTH` (`flower` service) | Web UI on container port 5555 |
| Next.js dashboard | `frontend/app/page.tsx:1` | `node server.js` inside the `frontend` image | Serves the read-only UI on port 3000 |
| CSV ingestion sweep | `docker-entrypoint.sh:20-30` | Container entrypoint | Moves `/app/imports/*.csv` to `.pending` for `system.import_csv` |
| Deployment validators | `scripts/validate_deployment.py:189`, `scripts/validate_startup.py:135` | Manual `python scripts/…` | Health + prerequisite checks |
| Chrome extension worker | `extension/background.js:1` | `chrome.action` popup / `chrome.alarms` | FOFA search-page harvest + upload |

Install / run: `docker compose up -d --build` after copying `.env.template` → `.env` (`README.md`, `docker-compose.yml`). Ports bound on the host: `API_PORT=8011`, `REDIS_PORT=6379`, `FLOWER_PORT=8555`, `FRONTEND_PORT=3000`, all on `127.0.0.1` (`docker-compose.yml`). Local dev alternative: `uvicorn app.api.main:app --reload --port 8001` after exporting the `.env` values.

## 5. Execution paths

### 5.1 Scan → validate → save
1. `celery beat` fires `scanner.scan_<source>` on its cron (e.g. `scan-github-4hours`, `app/workers/celery_app.py:415`).
2. `scanner.scan_github` at `app/workers/tasks/scanner_tasks.py:301` calls `_run_sync(_scan_github_async(...))`.
3. `_scan_github_async` iterates queries and invokes `github.search(...)` on the `GithubService` instance (`app/services/scanners.py:625`); each service returns `list[{"token": ..., "meta": {...}}]`.
4. Results funnel through `_save_credentials(results, source_name)` (`app/workers/tasks/scanner_tasks.py:167`), which enqueues `validation.validate_token` on the `validation` queue (`app/workers/tasks/scanner_tasks.py:126`).
5. `validation.validate_token` (`app/workers/tasks/validation_tasks.py:105`) acquires a Redis token via `_acquire_rate_token()`, calls `https://api.telegram.org/bot<token>/getMe`, then `getWebhookInfo` (line ~205), then persists or updates a row in `discovered_credentials` via `db.table(...)` (Supabase client at `app/core/database.py`).
6. On successful `getMe`, three pivot tasks are enqueued asynchronously: `search_github_user`, `search_bot_username`, `search_webhook_host` (`app/workers/tasks/pivot_tasks.py:89-183`).
7. If a new credential was inserted, `flow.enrich_credential` is queued to compute confidence score, member count and topic id (`app/workers/tasks/flow_tasks.py:759`).

### 5.2 Enrich → exfiltrate → broadcast
1. `flow.enrich_credential` (`app/workers/tasks/flow_tasks.py:759`) fetches bot capabilities, populates `meta.confidence_score`, `meta.chat_member_count`, `meta.topic_id`, and enqueues `flow.exfiltrate_chat`.
2. `flow.exfiltrate_chat` (`app/workers/tasks/flow_tasks.py:559`, queue `scrape`) hands off to `scraper_service` (`app/services/scraper_srv.py`).
3. `ScraperService.exfiltrate_chat` runs the four strategies defined in `app/services/_scraper/strategies.py`: `BotApiUpdateReader`, `TelethonHistoryReader`, `ForwardingArchiveReader`, `WebhookStateService`, orchestrated by `app/services/_scraper/lifecycle.py` and classified by `app/services/_scraper/results.py`.
4. New messages are inserted into `exfiltrated_messages` with the unique key `(credential_id, telegram_msg_id)` (`database/init.sql:98`). Telemetry indicators are also upserted into `telemetry_indicators` via `_index_telemetry_indicators` (`app/workers/tasks/flow_tasks.py:118`).
5. `flow.broadcast_pending` (`app/workers/tasks/flow_tasks.py:989`, scheduled every `BROADCAST_INTERVAL_MINUTES`) pulls rows from `exfiltrated_messages` where `is_broadcasted = FALSE`, claims each row atomically via `broadcast_claimed_at`, and calls `BroadcasterService.broadcast_message` (`app/services/broadcaster_srv.py`).
6. `BroadcasterService` decrypts the target credential's `bot_token` (`app/core/security.py`), instantiates a `telegram.Bot` (`python-telegram-bot`), and sends to `${MONITOR_GROUP_ID}` inside the appropriate forum topic. Failures are classified by `_classify_broadcast_exception` (`app/services/broadcaster_srv.py:63`) and written back to `broadcast_error`, `broadcast_attempts`, `next_retry_at`.
7. Successful sends mark `is_broadcasted = TRUE`; triggers `trg_monitor_stats_messages_delta` update the aggregate counter row (`database/init.sql:150-190`).

### 5.3 Honeypot receive → redirect
1. Public POST to `/honeypot/receive/{credential_id}` handled by `receive_webhook_update` (`app/api/routers/honeypot.py:62`).
2. Fail-closed if `HONEYPOT_MODE=False`; the `X-Telegram-Bot-Api-Secret-Token` header is compared against `HONEYPOT_SECRET`; per-credential opt-in is checked via `_honeypot_credential_allowed` reading `HONEYPOT_ALLOWLIST`.
3. Payload is written into `honeypot_updates` (migration `supabase/migrations/20260803000012_honeypot.sql`) and `_dispatch_alert(...)` is scheduled from `app/core/webhook.py`.
4. Beat entry `honeypot-redirect-sweep-30s` (`app/workers/celery_app.py:~355`) triggers `flow.honeypot_redirect_sweep` (`app/workers/tasks/flow_tasks.py:4002`).
5. When both `HONEYPOT_REDIRECT_MODE=True` and `HONEYPOT_REDIRECT_AUTHORIZED=True` (`app/core/config.py:36-46`), `flow.honeypot_redirect_one` (`app/workers/tasks/flow_tasks.py:4117`) issues `sendMessage` from the captured bot to the target user, pointing to `https://t.me/${HONEYPOT_REDIRECT_BOT}?start=${HONEYPOT_REDIRECT_DEEPLINK}`; multi-touch reminders are scheduled by `honeypot_redirect_tasks.py`.

## 6. Architecture
Directional dependencies observed from imports:

```
app/api/main.py ──▶ app/api/routers/{monitor,scan,ingest,health,media,honeypot}.py
                    │
                    ▼
             app/core/{config,auth,database,security,audit,redis_srv,retry,
                       circuit_breaker,queue_monitor,webhook,connectivity,...}
                    ▲                                              ▲
                    │                                              │
app/workers/celery_app.py ──▶ app/workers/tasks/*
                                  │
                                  ▼
                          app/services/*  (scanners*, scraper*, broadcaster_srv,
                                            bot_manager_srv, user_agent_srv,
                                            findings, entities, engagement, ...)
                                  │
                                  ▼
                          Telegram API (httpx / Telethon / python-telegram-bot)
                          Supabase REST (via supabase-py in app/core/database.py)
                          Redis (redis-py in app/core/redis_srv.py)
```

State lives in:
- Supabase Postgres: tokens (encrypted), messages, telemetry indicators, audit logs, telegram accounts, honeypot updates, finding alert policies, engagement/insight queue, monitor stats singleton.
- Redis: Celery broker + result backend, rate-limit token buckets (`rate_limit:telegram_getMe`), session leases, cooldowns, dedup keys (`validated:recent:<sha256>`), queue-monitor timestamps.
- Local filesystem inside containers: `/app/sessions/*.session` (Telethon), `/app/beat/celerybeat-schedule`, `/app/imports/`.

The layout is monolithic-Python-plus-frontend: one Python package (`app/`) split into `api`, `core`, `schemas`, `services`, `services/_scraper`, `utils`, `workers`, `workers/tasks`, `workers/tasks/_scanner`. Transport (routers), business logic (services + tasks) and storage adapters (`core/database.py`, `core/redis_srv.py`, `core/security.py`) are separated by directory. Task modules import both `services` and `core`; services import `core` and each other; `core` modules do not import from `services` or `workers` (checked by grep on `app/core/*.py`).

## 7. File inventory
Directory tree (top-level, depth 2 shown), sorted by path:

```
.
├── .agents/diagnosis/            6 markdown diagnosis notes
├── .claude/                      Local Claude/Kiro settings + scheduled task lock
├── .github/                      Dependabot, funding, labels, greetings + 7 workflows
├── .kiro/specs/scanner-query-coverage/  Kiro spec for a scanner bugfix
├── .playwright-mcp/              1 captured browser transcript
├── app/                          Main Python package
│   ├── api/                      FastAPI app + 6 routers
│   ├── core/                     Cross-cutting utilities and adapters (16 modules)
│   ├── schemas/                  Pydantic request/response models
│   ├── services/                 14 service modules + 5 scraper strategy modules
│   ├── utils/                    HTTP client + helpers
│   └── workers/                  Celery app + Flower app + 10 task modules
├── database/                     Canonical DDL + 8 patch migrations + retention SQL
├── docs/                         Honeypot doc, Cloudflare WAF, runbooks, 3 plan docs
├── extension/                    Manifest V3 Chrome extension (FOFA scraper)
├── frontend/                     Next.js 16 dashboard + Vitest suite
├── imports/                      README only; CSV drop-in target at runtime
├── scripts/                      15 operational and validation scripts
├── supabase/                     Config + 21 dated migrations
└── tests/                        59 test files (unit + integration + load + top-level)
```

Extension counts: 136 `.py`, 32 `.sql`, 27 `.md`, 15 `.yml`, 13 `.tsx`, 9 `.json`, 5 `.svg`, 5 `.png`, 4 `.html`, 4 `.ts`, 3 `.mjs`, 3 `.ps1`, 3 `.gitignore`, 3 `.sh`, 3 `.js`, 3 `.toml`, 2 `.bat`.

Selected files by directory (all 286 tracked files are on disk; grouped listing shown to respect line budget):

`app/api/`
- `app/api/__init__.py`
- `app/api/main.py`
- `app/api/routers/__init__.py`
- `app/api/routers/health.py`
- `app/api/routers/honeypot.py`
- `app/api/routers/ingest.py`
- `app/api/routers/media.py`
- `app/api/routers/monitor.py`
- `app/api/routers/scan.py`

`app/core/`
- `app/core/__init__.py`, `audit.py`, `auth.py`, `circuit_breaker.py`, `config.py`, `connectivity.py`, `constants.py`, `database.py`, `db_retry.py`, `logger.py`, `metrics.py`, `queue_monitor.py`, `redis_srv.py`, `retry.py`, `security.py`, `webhook.py`

`app/schemas/`
- `app/schemas/__init__.py`, `app/schemas/models.py`

`app/services/`
- `app/services/__init__.py`, `bot_listener.py`, `bot_manager_srv.py`, `broadcaster_srv.py`, `engagement.py`, `entities.py`, `finding_alerts.py`, `findings.py`, `scanners.py`, `scanners_extension.py`, `scraper_srv.py`, `telemetry_parser.py`, `topic_admin_srv.py`, `user_agent_srv.py`
- `app/services/_scraper/__init__.py`, `lifecycle.py`, `monitor_guard.py`, `results.py`, `strategies.py`

`app/utils/`
- `app/utils/__init__.py`, `helpers.py`, `http_client.py`

`app/workers/`
- `app/workers/__init__.py`, `celery_app.py`, `flower_app.py`
- `app/workers/tasks/__init__.py`, `audit_tasks.py`, `firehose_tasks.py`, `flow_tasks.py`, `honeypot_redirect_strategies.py`, `honeypot_redirect_tasks.py`, `import_tasks.py`, `pivot_tasks.py`, `scanner_tasks.py`, `validation_tasks.py`
- `app/workers/tasks/_scanner/__init__.py`, `base.py`, `queries.py`

`database/`
- `database/init.sql`, `database/rls_policies.sql`
- `database/operations/retention_cleanup.sql`
- `database/migrations/001_keepalive_grant.sql`, `2026-05-27-add-confidence-score.sql`, `2026-07-15-canonicalize-confidence-score-generated.sql`, `2026-07-17-create-rpc-atomic-patch.sql`, `2026-07-17-create-telemetry-indicators.sql`, `2026-08-02-scrape-broadcast-reliability.sql`, `2026-08-27-public-view-confidence-score.sql`, `2026-08-28-monitor-stats.sql`, `database/migrations/README.md`

`supabase/`
- `supabase/.gitignore`, `supabase/config.toml`
- `supabase/migrations/20260802000001_scrape_broadcast_reliability.sql`, `20260803000001_broadcasted_at.sql`, `20260803000010_message_fts.sql`, `20260803000011_media_hashes.sql`, `20260803000012_honeypot.sql`, `20260804000001_system_state.sql`, `20260805000001_account_membership_admin.sql`, `20260805000002_sender_user_id.sql`, `20260806000001_honeypot_redirect.sql`, `20260828000001_monitor_stats.sql`, `20260829000001_multi_touch_redirects.sql`, `20260903000001_supabase_optimization.sql`, `20260903000003_collection_yield_score.sql`, `20260903000004_rls_hardening.sql`, `20260903000005_discovered_credentials_public_authenticated.sql`, `20260904000001_disable_legacy_retention_jobs.sql`, `20260904000002_insight_queue.sql`, `20260904000003_entities_engagement.sql`, `20260904000004_finding_alert_policies.sql`, `20260904000005_monitor_findings_feedback.sql`, `20260906000001_dashboard_operator_authorization.sql`

`docs/`
- `docs/HONEYPOT.md`, `docs/cloudflare_waf_rules.md`, `docs/production_runbook.md`, `docs/telegram_probe_matrix.example.json`
- `docs/plans/2026-05-26-scanner-source-expansion.md`, `2026-05-27-hit-rate-expansion.md`, `2026-06-16-phase2-expansion.md`

`extension/`
- `extension/manifest.json`, `background.js`, `content.js`
- `extension/icons/icon128.png`, `icon16.png`, `icon48.png`
- `extension/ui/popup.html`, `popup.js`, `style.css`

`frontend/`
- `frontend/.dockerignore`, `.gitignore`, `Dockerfile`, `README.md`, `eslint.config.mjs`, `next.config.ts`, `package.json`, `package-lock.json`, `postcss.config.mjs`, `tsconfig.json`, `vercel.json`, `vite.config.ts`
- `frontend/app/globals.css`, `icon.png`, `layout.tsx`, `page.test.tsx`, `page.tsx`
- `frontend/app/signin/page.test.tsx`, `signin/page.tsx`
- `frontend/components/ChatWindow.test.tsx`, `ChatWindow.tsx`, `FindingsQueue.test.tsx`, `FindingsQueue.tsx`, `Sidebar.tsx`, `TelemetryAnalyticsView.tsx`
- `frontend/lib/auth.test.tsx`, `auth.tsx`, `supabase.ts`
- `frontend/public/file.svg`, `globe.svg`, `logo.png`, `next.svg`, `vercel.svg`, `window.svg`
- `frontend/test/setup.ts`

`scripts/`
- `scripts/backfill_all_media.py`, `backfill_media.py`, `backfill_via_telethon.py`, `get_account_names.py`, `logrotate.conf`, `ops_report.py`, `post_startup.py`, `release_gate.ps1`, `rotate_credentials.py`, `run_fofa_overnight.ps1`, `run_fofa_scan.mjs`, `schema_drift_check.py`, `setup_cloudflare_tunnel.ps1`, `setup_dev.sh`, `startup.bat`, `telegram_behavior_probe.py`, `TelegramHunter_Startup.xml`, `validate_deployment.py`, `validate_startup.py`

`tests/`
- Top-level: `tests/conftest.py`, `test_api.py`, `test_auth.py`, `test_error_hygiene.py`, `test_findings_api.py`, `test_scraper_restriction.py`, `test_security.py`, `test_supabase_rw.py`, `test_target_feed_export.py`
- `tests/integration/test_broadcaster.py`, `test_scanner_flow.py`, `test_scanners.py`
- `tests/load/test_health_load.py`
- `tests/unit/` — 46 files including `conftest.py`, `test_bot_identification.py`, `test_bot_listener_telemetry_command.py`, `test_bot_rotation.py`, `test_broadcast_failure_accounting.py`, `test_broadcaster_hardening.py`, `test_collection_yield_score.py`, `test_dashboard_operator_authorization.py`, `test_engagement_funnel.py`, `test_entities_engagement_migration.py`, `test_entities.py`, `test_exa_key_rotation.py`, `test_finding_alert_gate.py`, `test_finding_alert_migration.py`, `test_finding_alerts.py`, `test_findings.py`, `test_gateway_telemetry_probe.py`, `test_helpers.py`, `test_honeypot_redirect_bugs.py`, `test_honeypot_redirect_gate.py`, `test_infrastructure_context.py`, `test_insight_queue_migration.py`, `test_media_file_meta_extraction.py`, `test_monitor_findings_feedback_migration.py`, `test_on_demand_disconnect.py`, `test_queue_monitor.py`, `test_retention_safety.py`, `test_retry.py`, `test_rls_auth_hardening.py`, `test_runtime_regressions.py`, `test_scanner_preservation.py`, `test_scanner_query_coverage_bug.py`, `test_schema_drift_check.py`, `test_scrape_classification_persistence.py`, `test_scrape_result_classification.py`, `test_scraper_orchestration.py`, `test_searchcode_infrastructure_context.py`, `test_telegram_client_lifecycle.py`, `test_telemetry_indicator_indexing.py`, `test_telemetry_parser.py`, `test_test_harness_integrity.py`, `test_transient_media_archiver.py`, `test_user_agent_invite_hardening.py`, `test_validate_deployment.py`, `test_webhook_dispatch.py`, `test_webhook_state_service.py`.

Top-level: `.deepsource.toml`, `.dockerignore`, `.env.template`, `.gitignore`, `.pre-commit-config.yaml`, `.sourcery.yml`, `AUDIT.md`, `AUDIT_LOG.md`, `Dockerfile`, `LICENSE`, `NOTICE`, `PRD.md`, `README.md`, `SUPABASE_KEEPALIVE_SETUP.md`, `bugfix.md`, `competitive-upgrade-plan-CORRECTED.html`, `competitive-upgrade-plan.html`, `design.md`, `docker-compose.prod.yml`, `docker-compose.yml`, `docker-entrypoint.sh`, `package-lock.json`, `package.json`, `plan.html`, `plan (root-level HTML artifacts of an earlier planning pass — not build inputs)`, `pyproject.toml`, `requirements-dev.txt`, `requirements.txt`, `security_audit.md`, `start.bat`, `start.sh`, `tasks.md`, `imports/README.md`.

Working-tree untracked files: none (git status was empty). Generated files inside the repo: `frontend/package-lock.json` and root `package-lock.json` (npm-managed lockfiles) — kept in tree; nothing else appears generated.

## 8. Key modules in depth

### `app/api/` — FastAPI HTTP layer
- Responsibility: exposes 25 HTTP endpoints across six routers (see Section 9). Applies CORS, `slowapi` rate limiting (Redis-backed, `_rate_key` function in `app/api/main.py:114`), `X-Monitor-Key` gating via `app.core.auth.require_monitor_key`.
- Key files and symbols: `app/api/main.py` (`lifespan`, `app = FastAPI(...)`, `include_router`), `app/api/routers/monitor.py`, `.../scan.py`, `.../ingest.py`, `.../health.py`, `.../media.py`, `.../honeypot.py`.
- Depends on: `app.core.config`, `app.core.auth`, `app.core.database`, `app.core.security`, `app.core.audit`, `app.schemas.models`, `app.services.finding_alerts`, `app.services.engagement`, `app.workers.celery_app`, `app.workers.tasks.flow_tasks`, `slowapi`, `fastapi`.
- Depended on by: none inside the app (top of the transport tree).
- Notable behaviour: `/docs`, `/redoc`, `/openapi.json`, `/scan/trigger` all return 404/403 when `ENV=production`. Lifecycle hooks POST a startup/shutdown notice through `BroadcasterService.send_log` (`app/api/main.py:35-95`). Rate limit bucket is derived from `X-Monitor-Key` via `hmac.compare_digest` to prevent forged-key bucket manipulation (`app/api/main.py:114-136`).
- Tests covering it: `tests/test_api.py`, `tests/test_findings_api.py`, `tests/test_target_feed_export.py`, `tests/test_error_hygiene.py`, `tests/test_auth.py`.

### `app/core/` — shared adapters and cross-cutting utilities
- Responsibility: configuration loading (Pydantic Settings with `.env`), Supabase client singleton, Fernet/MultiFernet encryption, Redis service helpers, retry decorator, per-service circuit breakers, in-memory metrics, structured audit logger with token redaction, connectivity gate, queue-monitor bookkeeping, alert webhook dispatch, DB retry helpers.
- Key files and symbols: `config.py:Settings`, `database.py:db`, `security.py:SecurityService`, `redis_srv.py`, `retry.py`, `circuit_breaker.get_circuit_breaker`, `audit.py:AuditLogger`, `queue_monitor.py`, `webhook.py:dispatch_alert`, `auth.py:require_monitor_key`, `connectivity.py:wait_for_internet_sync`.
- Depends on: `pydantic-settings`, `cryptography.fernet.MultiFernet`, `supabase`, `redis`, `httpx`.
- Depended on by: every router, every task module, every service.
- Notable behaviour: `SecurityService` supports rotation via `ENCRYPTION_KEY_LEGACY` (`app/core/security.py`); `AuditLogger` redacts bot tokens in log strings before persisting to `audit_logs`; `require_monitor_key` uses constant-time compare on `MONITOR_API_KEY`.
- Tests: `tests/test_security.py`, `tests/test_auth.py`, `tests/unit/test_retry.py`, `tests/unit/test_queue_monitor.py`, `tests/unit/test_webhook_dispatch.py`, `tests/unit/test_rls_auth_hardening.py`.

### `app/services/scanners.py` and `app/services/scanners_extension.py`
- Responsibility: HTTP clients for OSINT sources. Twenty-one scanner service classes across the two files (`ShodanService`, `FofaService`, `UrlScanService`, `GithubService`, `GitlabService`, `ExaService`, `WaybackService`, `CommonCrawlService`, `SourcegraphService`, `GithubGistService`, `GrepAppService`, `PublicWwwService`, `GoogleSearchService`, `BitbucketService`, `PastebinService`, `RentryService`, `HastebinService`, `NetlasService`, `ReplitService`, `PostmanService`, `SearchcodeService`). All return `list[{"token": ..., "meta": {...}}]`.
- Shared utilities exported at the top of `scanners.py`: `TOKEN_PATTERN` regex (`digits:35chars`), `_is_valid_token`, `_perform_active_deep_scan`, `retry_with_backoff`, `SPOOFED_HEADERS`.
- Depends on: `httpx`, `urllib3`, `app.core.config`, `app.utils.http_client.get_async_http_client`.
- Depended on by: `app/workers/tasks/scanner_tasks.py`, indirectly `pivot_tasks.py` and `firehose_tasks.py`.
- Notable behaviour: rate-limit / backoff via `retry_with_backoff`; each scanner degrades to `[]` when its API key is missing (own reads of `settings.<KEY>`). Netlas rotates two accounts using Redis daily counters (`scanners_extension.NetlasService`).
- Tests: `tests/integration/test_scanners.py`, `tests/unit/test_exa_key_rotation.py`, `tests/unit/test_scanner_preservation.py`, `tests/unit/test_scanner_query_coverage_bug.py`, `tests/unit/test_searchcode_infrastructure_context.py`, `tests/unit/test_gateway_telemetry_probe.py`.

### `app/services/scraper_srv.py` + `app/services/_scraper/`
- Responsibility: retrieves chat history for a captured bot. Four strategies live in `_scraper/strategies.py` (`BotApiUpdateReader`, `TelethonHistoryReader`, `ForwardingArchiveReader`, `BotPreflightService`, `MessageIdReader`, `UserAgentJoinService`, `WebhookStateService`). Orchestration and lifecycle in `_scraper/lifecycle.py`; classification of outcomes in `_scraper/results.py` (`ScrapeResult`, `ScrapeReason`, `StrategyAttempt`); monitor-group guard in `_scraper/monitor_guard.py` prevents accidental scrapes of `MONITOR_GROUP_ID`.
- Key symbols: `scraper_service` module-level singleton, `ScrapeResultClassifier`, `_bot_api_media_info`, `_resolve_history_result`.
- Depends on: `telethon`, `httpx`, `app.core.database`, `app.core.security`, `app.core.config`, `app.utils.http_client`.
- Depended on by: `flow_tasks.exfiltrate_chat`, `bot_listener` (for the `/starthunter` interactive login flow through `user_agent_srv`).
- Notable behaviour: enforces `TELEGRAM_HISTORY_TIMEOUT_SECONDS` per scrape, disconnects Telethon clients under `TELEGRAM_CLIENT_DISCONNECT_TIMEOUT_SECONDS`, distinguishes AuthKey/FloodWait/UserDeactivatedBan; own monitor-bot tokens are refused before any API call.
- Tests: `tests/test_scraper_restriction.py`, `tests/unit/test_scraper_orchestration.py`, `tests/unit/test_scrape_classification_persistence.py`, `tests/unit/test_scrape_result_classification.py`, `tests/unit/test_telegram_client_lifecycle.py`, `tests/unit/test_transient_media_archiver.py`.

### `app/services/broadcaster_srv.py`
- Responsibility: sends messages into forum topics of `MONITOR_GROUP_ID` using `python-telegram-bot`. Encapsulates token decryption, media filename synthesis, MIME registration for `.apk`/`.apks`/`.xapk`, and exception → `BroadcastSendError` classification.
- Depends on: `telegram` (python-telegram-bot), `app.core.database`, `app.core.security`, `app.services.user_agent_srv.user_agent`.
- Depended on by: `flow_tasks.broadcast_pending`, `flow_tasks._send_alert`, `api/main.lifespan`, `celery_app._send_signal_log`, `bot_listener`.
- Notable behaviour: `_classify_broadcast_exception` maps `RetryAfter`, `Forbidden`, `BadRequest` (topic missing, message thread not found), timeouts; retry policy uses `broadcast_error`, `broadcast_attempts`, `next_retry_at` columns and honours `BROADCAST_RETRY_MAX_DELAY_SECONDS` + `BROADCAST_RETRY_JITTER_RATIO`.
- Tests: `tests/integration/test_broadcaster.py`, `tests/unit/test_broadcast_failure_accounting.py`, `tests/unit/test_broadcaster_hardening.py`.

### `app/services/bot_listener.py`
- Responsibility: long-running `python-telegram-bot` polling loop that binds admin commands, an `ConversationHandler` for `/starthunter`, DM opt-out logic, and a `/tmp/bot_alive` heartbeat file used by the container healthcheck.
- Depends on: `python-telegram-bot`, `redis.asyncio`, `app.services.scraper_srv`, `app.services.user_agent_srv`, `app.core.audit`.
- Depended on by: none inside the app; started directly by the `bot` container.
- Notable behaviour: 1 381 lines; commands registered at lines 1419-1432 (`/start`, `/stop`, `/optout`, `/unsubscribe`, `/status`, `/pause`, `/resume`, `/restart`, `/help`, `/commands`, `/bots`, `/telemetry`, `/indicators`, `/getfile`, `/archive`, `/backfill`, `/starthunter` via ConversationHandler, `/cancel`); multi-bot rotation with per-poll Redis lock; sweeps orphan `temp_login_*.session*` files on startup.
- Tests: `tests/unit/test_bot_listener_telemetry_command.py`, `tests/unit/test_bot_identification.py`, `tests/unit/test_bot_rotation.py`.

### `app/workers/celery_app.py`
- Responsibility: Celery app definition, persistent event loop per worker, queue routing, and the entire beat schedule.
- Depends on: `celery`, `redis`, `app.core.config`, `app.core.connectivity`, `app.core.audit`, `app.core.queue_monitor`, `app.services.broadcaster_srv`.
- Depended on by: every task module through `from app.workers.celery_app import app`.
- Notable behaviour: `get_worker_loop`/`_run_sync` are the canonical async-in-Celery bridge; `@task_failure.connect` writes permanent failures to `audit_logs`; `@task_prerun.connect` gates task start on `check_internet` (except `flow.system_heartbeat`); `task_routes` sends `scanner.*` to `scanners`, `validation.*` and `pivot.*` to `validation`, `firehose.*` to `scanners`, and explicit `flow.exfiltrate_chat`/`flow.rescrape_active` to `scrape`.
- Tests: `tests/unit/test_queue_monitor.py`, `tests/unit/test_runtime_regressions.py`.

### `app/workers/tasks/flow_tasks.py`
- Responsibility: 3 864-line orchestration hub. Defines 33 `@app.task` handlers (enumerated in Section 9 execution paths) covering enrichment, exfiltration, broadcasting, canary flow, webhook probing/takeover, media forensics, findings/entity queues, attribution, honeypot redirect sweeps, reconciliation, group-membership audit and system heartbeat.
- Depends on: `app.core.*`, `app.services.scraper_srv`, `app.services.broadcaster_srv`, `app.services.telemetry_parser`, `app.services.findings`, `app.services.finding_alerts`, `app.services.entities`, `app.services.engagement`, `app.services.user_agent_srv`, `httpx`, `redis`.
- Depended on by: `scanner_tasks.py` (imports `redis_client`, `get_broadcaster`, `async_execute`, `enrich_credential`), `validation_tasks.py`, `pivot_tasks.py`, `firehose_tasks.py`, `honeypot_redirect_tasks.py`, `api/routers/ingest.py`, `api/routers/scan.py`.
- Notable behaviour: `broadcast_pending` claims rows via `broadcast_claimed_at`; `hash_exfil_media` uses Pillow + imagehash for perceptual hashing; `probe_webhooks` + `force_webhook_takeover_pass` implement the webhook takeover pipeline; `honeypot_redirect_one` gates on both `HONEYPOT_REDIRECT_MODE` and `HONEYPOT_REDIRECT_AUTHORIZED`.
- Tests: `tests/unit/test_honeypot_redirect_bugs.py`, `test_honeypot_redirect_gate.py`, `test_finding_alert_gate.py`, `test_finding_alerts.py`, `test_engagement_funnel.py`, `test_scraper_orchestration.py` and others.

### `app/workers/tasks/scanner_tasks.py`, `validation_tasks.py`, `pivot_tasks.py`, `firehose_tasks.py`
- Responsibility: OSINT source drivers + validation queue + pivot fan-out + GitHub Events firehose.
- `scanner_tasks.py` — 22 `scanner.scan_*` tasks (see Section 9) plus `scanner.retry_cold`.
- `validation_tasks.py` — `validation.validate_token`, `validation.refresh_pending_tokens`, `validation.backfill_scoring`; Redis token bucket `rate_limit:telegram_getMe` with configurable `VALIDATE_RATE_MAX` / `VALIDATE_RATE_WINDOW` / `VALIDATE_RATE_MAX_WAIT`.
- `pivot_tasks.py` — `pivot.search_github_user`, `pivot.search_bot_username`, `pivot.search_webhook_host` (fired after each successful `getMe`).
- `firehose_tasks.py` — `firehose.poll_github_events` (ETag-aware GitHub public event polling every 30 s).
- Depends on: `scanners`, `scanners_extension`, `flow_tasks.async_execute`, `flow_tasks.redis_client`, `app.core.security`, `app.core.database`.
- Tests: `tests/integration/test_scanner_flow.py`, `tests/unit/test_scanner_preservation.py`, `test_scanner_query_coverage_bug.py`, `test_exa_key_rotation.py`, `test_searchcode_infrastructure_context.py`.

### `app/workers/tasks/audit_tasks.py`
- Responsibility: hourly monitor-group integrity checks (`audit.audit_active_topics`), 90-min `system.self_heal`, `system.enforce_whitelist`, `system.cleanup_general_topic`, `audit.prune_audit_logs` weekly, `audit.cleanup_matkap_bots`, `system.backfill_general_messages`.
- Depends on: Telegram Bot API through `httpx`, `app.services.topic_admin_srv`, `app.core.audit`.
- Tests: `tests/unit/test_test_harness_integrity.py`, `tests/unit/test_retention_safety.py`.

### `frontend/`
- Responsibility: Next.js 16 read-only dashboard using Supabase JS client with authenticated read access. Views: `page.tsx` (findings, chat, bot telemetry, global telemetry); `signin/page.tsx`; components `Sidebar`, `ChatWindow`, `FindingsQueue`, `TelemetryAnalyticsView`; auth context in `lib/auth.tsx`; Supabase client in `lib/supabase.ts`.
- Depends on: `@supabase/supabase-js ^2.89`, `next ^16.2.4`, `react 19.2.3`, `lucide-react`, `tailwindcss ^4`, `clsx`, `tailwind-merge`.
- Depended on by: nothing inside the repo.
- Tests: `frontend/app/page.test.tsx`, `signin/page.test.tsx`, `components/ChatWindow.test.tsx`, `components/FindingsQueue.test.tsx`, `lib/auth.test.tsx`, `frontend/test/setup.ts` (Vitest + jsdom).

## 9. External interfaces

HTTP routes (grep on `@router.<verb>(` in `app/api/routers/`):

| Method | Path | Handler | Notes |
|---|---|---|---|
| GET | `/` | `app/api/main.py:read_root` | returns `{status}` |
| GET | `/health/` | `app/api/routers/health.py:165` | liveness |
| GET | `/health/detailed` | `health.py:174` | monitor-key gated |
| GET | `/health/metrics` | `health.py:228` | monitor-key |
| GET | `/health/queues` | `health.py:241` | monitor-key |
| GET | `/health/operational` | `health.py:266` | monitor-key |
| GET | `/health/circuit-breakers` | `health.py:309` | monitor-key |
| POST | `/health/circuit-breakers/{name}/reset` | `health.py:321` | monitor-key |
| GET | `/health/quotas` | `health.py:355` | monitor-key |
| GET | `/health/bot-pool` | `health.py:393` | monitor-key |
| GET | `/monitor/stats` | `monitor.py:42` | cached 30 s |
| GET | `/monitor/credentials` | `monitor.py:120` |  |
| GET | `/monitor/messages` | `monitor.py:186` |  |
| GET | `/monitor/findings` | `monitor.py:198` |  |
| GET | `/monitor/findings/{finding_id}` | `monitor.py:258` |  |
| GET (2nd) | `/monitor/findings/...` variant | `monitor.py:245` | see file |
| POST | `/monitor/findings/{...}` | `monitor.py:280` | feedback |
| POST | `/monitor/engagement/lifecycle` | `monitor.py:320` |  |
| GET | `/monitor/export` | `monitor.py:350` | CSV / JSON export |
| GET | `/monitor/broadcasts/pending` | `monitor.py:395` |  |
| POST | `/monitor/broadcasts/{message_id}/retry` | `monitor.py:458` |  |
| POST | `/monitor/topics/revoked/close` | `monitor.py:519` |  |
| GET | `/monitor/webhooks` | `monitor.py:555` |  |
| GET | `/monitor/targets/export` | `monitor.py:607` |  |
| GET | `/monitor/search` | `monitor.py:749` | FTS across messages |
| GET | `/monitor/operators` | `monitor.py:821` | C2 operator clusters |
| POST | `/scan/trigger` | `scan.py:15` | dev-only (403 in production) |
| POST | `/ingest/extension/credentials` | `ingest.py:58` | monitor-key |
| POST | `/ingest/tokens` | `ingest.py:212` | monitor-key |
| GET | `/media/{message_id}` | `media.py:15` | monitor-key |
| POST | `/honeypot/receive/{credential_id}` | `honeypot.py:62` | header-secret gated |
| GET | `/honeypot/status` | `honeypot.py:151` | monitor-key |

Celery task registrations (78 total via `@app.task`, 62 with explicit `name=`; enumerated queues and cron in `app/workers/celery_app.py:220-500`): `flow.*` (33), `scanner.*` (22), `audit.*`/`system.*` (7), `validation.*` (3), `pivot.*` (3), `firehose.*` (1), `honeypot redirect helper tasks` (2), `system.import_csv` (1). Full name list at `app/workers/tasks/*.py`.

Telegram admin commands (`app/services/bot_listener.py:1419-1445`): `/start`, `/stop`, `/optout`, `/unsubscribe`, `/status`, `/pause`, `/resume`, `/restart`, `/help`, `/commands`, `/bots`, `/telemetry`, `/indicators`, `/getfile`, `/archive`, `/backfill`, `/starthunter` (ConversationHandler), `/cancel`.

Outbound integrations reached from code: Telegram Bot API (`https://api.telegram.org`), Telegram MTProto via Telethon, Shodan REST, FOFA REST, URLScan.io, GitHub REST (code search, gists, events), GitLab REST, Bitbucket API, Exa search, Serper/Google Custom Search, PublicWWW, Netlas, Sourcegraph, Postman, Replit (disabled), Wayback Machine, Common Crawl, DockerHub, `grep.app`, `pastebin.com`, `rentry.co`, `hastebin`, Supabase REST, Redis, optional `ALERT_WEBHOOK_URL`, optional Cloudflare Tunnel target for honeypot.

## 10. Data and configuration

Tables declared in `database/init.sql`: `discovered_credentials`, `exfiltrated_messages`, `monitor_stats`, `telemetry_indicators`, `telegram_accounts`, `audit_logs`, `keepalive_log`. Triggers `trg_monitor_stats_credentials_delta` / `trg_monitor_stats_messages_delta` maintain the singleton `monitor_stats` row. Function `get_monitor_stats()` is `SECURITY DEFINER`, granted to `service_role`, revoked from `PUBLIC`. `discovered_credentials.confidence_score` and `chat_member_count` are `GENERATED ALWAYS AS STORED` from the `meta` JSONB column.

Additional objects introduced by the 21 dated `supabase/migrations/` files include: broadcast reliability columns (`20260802000001`), `broadcasted_at` (`20260803000001`), message FTS index (`20260803000010`), `media_hashes` (`20260803000011`), `honeypot_updates` (`20260803000012`), `system_state` (`20260804000001`), `account_membership_admin` (`20260805000001`), `sender_user_id` (`20260805000002`), `honeypot_redirect_log` (`20260806000001`), `monitor_stats` (`20260828000001`), multi-touch redirect tables (`20260829000001`), `collection_yield_score` (`20260903000003`), RLS hardening (`20260903000004`), `discovered_credentials_public` view (`20260903000005`), retention job disable (`20260904000001`), insight queue (`20260904000002`), entities + engagement (`20260904000003`), finding alert policies (`20260904000004`), monitor findings feedback (`20260904000005`), dashboard operator claim (`20260906000001`).

RLS policies (`database/rls_policies.sql`): service-role bypasses; anon can never `SELECT` `discovered_credentials` (frontend reads a `discovered_credentials_public` view); Chrome extension `INSERT`/`UPDATE` gated by `x-extension-secret` header matched against `current_setting('app.extension_write_secret')`.

Environment variable table (names only; defaults per `app/core/config.py` and `.env.template`):

| Name | Read at | Default |
|---|---|---|
| `PROJECT_NAME` | `app/core/config.py:8` | `Telegram Hunter` |
| `ENV` | `config.py:9` | `development` |
| `DEBUG` | `config.py:10` | `True` |
| `SUPABASE_URL` | `config.py:13`, `database.py` | required |
| `SUPABASE_KEY` | `config.py:14`, frontend build | required |
| `SUPABASE_SERVICE_ROLE_KEY` | `config.py:15`, `database.py` | required |
| `REDIS_URL` | `config.py:16`, `celery_app.py:32`, `flower_app.py:8` | required |
| `ENCRYPTION_KEY` | `config.py:19`, `security.py` | required, 44 chars |
| `ENCRYPTION_KEY_LEGACY` | `security.py` | `""` |
| `PSEUDONYMIZATION_KEY` | `config.py:26` | `""` |
| `ALLOW_PUBLIC_STARTHUNTER` | `config.py:31` | `False` |
| `HONEYPOT_REDIRECT_MODE` | `config.py:42` | `True` |
| `HONEYPOT_REDIRECT_AUTHORIZED` | `config.py:43` | `False` |
| `HONEYPOT_REDIRECT_BOT` | `config.py:44` | `bryanseahbot` |
| `HONEYPOT_REDIRECT_DEEPLINK` | `config.py:45` | `migrate` |
| `MONITOR_API_KEY` | `config.py:46`, `auth.py` | required |
| `ALERT_WEBHOOK_URL` | `config.py:47`, `webhook.py` | `""` |
| `ALERT_WEBHOOK_SECRET` | `config.py:48` | `""` |
| `ENABLE_LEGACY_EVENT_ALERTS` | `config.py:50` | `False` |
| `FINDING_ALERTS_ENABLED` | `config.py:52` | `False` |
| `MONITOR_BOT_TOKEN` | `config.py:57`, `bot_listener.py` | required (comma-separated) |
| `MONITOR_GROUP_ID` | `config.py:58`, `broadcaster_srv.py` | required |
| `WHITELISTED_BOT_IDS` | `config.py:59` | `""` |
| `ANONYMOUS_ADMIN_ID` | `config.py:60` | `1087968824` |
| `TELEGRAM_API_ID` | `config.py:84` | required |
| `TELEGRAM_API_HASH` | `config.py:85` | required |
| `AUTO_ARCHIVE_MEDIA` | `config.py:61` | `False` |
| `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE` | `config.py:68` | `False` |
| `TELEGRAM_HISTORY_TIMEOUT_SECONDS` | `config.py:69` | `90.0` |
| `TELEGRAM_CLIENT_DISCONNECT_TIMEOUT_SECONDS` | `config.py:70` | `10.0` |
| `AUTO_CLOSE_REVOKED_TOPICS` | `config.py:71` | `True` |
| `CANARY_CREDENTIAL_ID` | `config.py:77` | `None` |
| `CANARY_EXPECTED_TEXT` | `config.py:78` | `telegramhunter-canary` |
| `CANARY_MAX_AGE_SECONDS` | `config.py:79` | `1800` |
| `CANARY_STALE_SECONDS` | `config.py:80` | `3600` |
| `PUBLIC_FRONTEND_URL` | `config.py:81` | `None` |
| `DATABASE_URL` | `config.py:82`, `scripts/schema_drift_check.py` | `None` |
| `ENABLE_RAW_MESSAGE_BROADCAST` | `config.py:86` | `False` |
| `QUEUE_ALERT_LENGTH_THRESHOLD` | `config.py:87` | `100` |
| `QUEUE_ALERT_OLDEST_AGE_SECONDS` | `config.py:88` | `900` |
| `OPERATIONAL_REPORT_WINDOW_HOURS` | `config.py:89` | `24` |
| `BROADCAST_FAILURE_ALERT_THRESHOLD` | `config.py:90` | `5` |
| `SCRAPE_REASON_ALERT_THRESHOLD` | `config.py:91` | `10` |
| `TELEGRAM_LOG_MIN_INTERVAL_SECONDS` | `config.py:92` | `2.0` |
| `HONEYPOT_MODE` | `config.py:104` | `False` |
| `HONEYPOT_WEBHOOK_URL` | `config.py:105` | `None` |
| `HONEYPOT_SECRET` | `config.py:106` | `None` |
| `HONEYPOT_ALLOWLIST` | `config.py:107` | `""` |
| `PROTECTED_BOT_IDS` | `config.py:110` | `""` |
| `SHODAN_KEY`, `FOFA_EMAIL`, `FOFA_KEY`, `URLSCAN_KEY`, `GITHUB_TOKEN`, `GITHUB_TOKENS`, `GITLAB_TOKEN`, `BITBUCKET_USER`, `BITBUCKET_API_TOKEN`, `EXA_API_KEY`, `EXA_API_KEY_2`, `EXA_API_KEY_3`, `CENSYS_ID`, `CENSYS_SECRET`, `HYBRID_ANALYSIS_KEY`, `GOOGLE_SEARCH_KEY`, `GOOGLE_CSE_ID`, `PUBLICWWW_KEY`, `POSTMAN_API_KEY`, `NETLAS_API_KEY_1`, `NETLAS_API_KEY_2`, `SERPER_API_KEY` | `config.py:88-140`, `scanners*.py` | all `None`/`""` |
| `TELETHON_PROXY_URL`, `HTTP_PROXY_URL` | `config.py:142-143` | `None` |
| `TARGET_COUNTRIES` | `config.py:150` | 46-code list |
| `BROADCAST_INTERVAL_MINUTES`, `RESCRAPE_INTERVAL_HOURS`, `SCAN_INTERVAL_HOURS`, `AUDIT_INTERVAL_HOURS`, `REVOKED_TOPIC_CLOSE_INTERVAL_MINUTES` | `celery_app.py:beat_schedule` | 1 / 1 / 4 / 2 / 5 |
| `VALIDATE_RATE_MAX`, `VALIDATE_RATE_WINDOW`, `VALIDATE_RATE_MAX_WAIT` | `validation_tasks.py:43-46` | 30 / 10 / 30.0 |
| `BROADCAST_BATCH_SIZE`, `BROADCAST_RETRY_MAX_DELAY_SECONDS`, `BROADCAST_RETRY_JITTER_RATIO` | `flow_tasks.broadcast_pending` | 200 / 86400 / 0.20 |
| `RESCRAPE_BACKPRESSURE_THRESHOLD` | `flow_tasks.rescrape_active` | 100 |
| `FLOWER_BASIC_AUTH` | `docker-compose.yml`, service refuses `admin:changeme` | required |
| `EXTRA_CORS_ORIGINS` | `api/main.py:150` | `""` |
| `API_PORT`, `REDIS_PORT`, `FLOWER_PORT`, `FRONTEND_PORT`, `COMPOSE_PROJECT_NAME` | `docker-compose.yml` | 8011 / 6379 / 8555 / 3000 / `theprawnhunter` |
| `REDIS_VOLUME_NAME`, `SESSIONS_VOLUME_NAME`, `IMPORTS_VOLUME_NAME`, `BEAT_SCHEDULE_VOLUME_NAME` | `docker-compose.yml volumes:` | `telegramhunter_*` |
| `API_WORKERS`, `API_HEALTHCHECK_*`, `WORKER_*_CONCURRENCY`, per-service `*_CPUS` / `*_MEM_LIMIT` | `docker-compose.yml`, `docker-compose.prod.yml` | see file |
| `EXTENSION_WRITE_SECRET` | `.env.template`, referenced in `database/rls_policies.sql` comment | required for extension direct-write path |
| `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_KEY` | `frontend/lib/supabase.ts`, `frontend/Dockerfile` | build-arg |
| `TELEGRAM_PROBE_*` (14 variables) | `scripts/telegram_behavior_probe.py`, `config.py` | all blank (probe returns skipped matrix) |
| `GH_OSINT_TOKEN` | `.github/workflows/ci.yml`, aliased into `GITHUB_TOKEN` by `Settings.resolve_token_aliases` (`config.py:157`) | injected by CI |

Config precedence: Pydantic Settings first reads `.env` next to the source root, then real environment variables override, then per-service `env_file: .env` in Docker Compose. `EXTENSION_WRITE_SECRET` lives only in the Supabase DB parameter `app.extension_write_secret` (`database/rls_policies.sql:33-45`).

Secrets: `.env` is not tracked (in `.gitignore`); `.env.template` contains placeholder names only. No literal secret values were observed in tracked files.

## 11. Dependencies
Runtime Python (`requirements.txt`, 20 pinned lines): `fastapi==0.136.0`, `slowapi==0.1.9`, `uvicorn==0.44.0`, `gunicorn==23.0.0`, `pydantic>=2.10,<3`, `pydantic-settings>=2.6,<3`, `supabase==2.28.3`, `httpx[socks]==0.28.1`, `celery==5.6.3`, `flower==2.0.1`, `tornado>=6.4.2`, `redis==7.4.0`, `python-telegram-bot[job-queue]==22.7`, `Telethon==1.43.2`, `pyasn1>=0.6.4`, `cryptg==0.4.0`, `cryptography==46.0.7`, `requests==2.33.1`, `python-dotenv==1.0.0`, `netlas==0.8.2`, `PySocks>=1.7.1`, `pytest==8.3.5`, `pytest-asyncio==0.24.0`, `hypothesis==6.131.11`, `Pillow==11.0.0`, `imagehash==4.3.1`, plus Snyk-pinned `tornado>=6.5.7` and `zipp>=3.19.1`.

Dev extras (`requirements-dev.txt`): `pytest-mock==3.12.0`, `pytest-cov==4.1.0`, `ruff==0.1.9`, `mypy==1.8.0`, `types-requests==2.31.0.10`.

Frontend dependencies (`frontend/package.json`): runtime — `@supabase/supabase-js ^2.89`, `next ^16.2.4`, `react 19.2.3`, `react-dom 19.2.3`, `clsx ^2.1.1`, `lucide-react ^0.562.0`, `tailwind-merge ^3.4.0`. Dev — `@tailwindcss/postcss ^4`, `@testing-library/jest-dom ^6.4`, `@testing-library/react ^16`, `@testing-library/user-event ^14.5`, `@types/node ^20`, `@types/react ^19`, `@types/react-dom ^19`, `@vitejs/plugin-react ^4.2`, `eslint ^9`, `eslint-config-next 16.1.1`, `jsdom ^24`, `tailwindcss ^4`, `typescript ^5`, `vitest ^2.0.0`.

Root Node package (`package.json`) exists only for the FOFA scan helper: `puppeteer-core ^25.5.0`. Scripts: `check` (Node syntax check on `scripts/run_fofa_scan.mjs`), `fofa:scan`, `test` (alias for `check`).

Lockfiles present: `frontend/package-lock.json` (9 092 lines), root `package-lock.json`. No Python lockfile is tracked — versions pinned in `requirements*.txt` are exact.

## 12. Testing and CI

Python tests: 59 test files. `tests/conftest.py` seeds a synthetic `.env` (mock Supabase URL, generated Fernet key, `MONITOR_BOT_TOKEN=123:ABC,456:DEF,789:GHI`) before importing `app.api.main`. Markers registered in `pyproject.toml`: `unit`, `integration`, `live`, `load`, `slow`. `pytest.ini` block sets `addopts = -v --tb=short --strict-markers`. Coverage source configured to `app` in `[tool.coverage.run]`.

Frontend tests: `npm test` runs `tsc --noEmit` then `vitest run` with `frontend/test/setup.ts` (jsdom + `@testing-library/jest-dom`). ESLint via `eslint-config-next`.

CI workflows:
- `.github/workflows/ci.yml` — three jobs on `push`/`pull_request` to `main`: `test` (Python 3.11, installs `requirements.txt` + `requirements-dev.txt`, runs `pytest` with secrets injected as env vars); `quality` (`ruff==0.1.9` on `.`); `frontend` (Node 22, `npm ci`, `npm test`, `npm run lint`, `npm run build`).
- `.github/workflows/bandit.yml` — SARIF upload to Code Scanning; runs on Python changes and weekly Monday cron.
- `.github/workflows/semgrep.yml` — `p/default`, `p/security-audit`, `p/secrets`; SARIF upload; weekly cron.
- `.github/workflows/trufflehog.yml` — daily 15:00 UTC + push/PR; `--only-verified`.
- `.github/workflows/supabase-keep-alive.yml` — daily 08:00 UTC INSERT+DELETE against `keepalive_log`.
- `.github/workflows/labeler.yml`, `greetings.yml` — housekeeping.

Local pre-commit (`.pre-commit-config.yaml`): `ruff` + `ruff-format`, `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-added-large-files (--maxkb=1000)`, `check-json`, `check-toml`, `detect-private-key`.

Additional analyzers registered: `.deepsource.toml` enables `python` and `docker`; `.sourcery.yml` is present.

Coverage gaps observed by absence of test file names in `tests/`: `app/services/bot_manager_srv.py`, `app/services/finding_alerts.py` (has partial coverage), `app/workers/tasks/firehose_tasks.py`, `app/workers/tasks/pivot_tasks.py`, `app/workers/tasks/import_tasks.py`, `extension/`, `scripts/*`.

## 13. Branches

| Branch | Last commit (date) | Author | Ahead / behind `main` | Apparent purpose |
|---|---|---|---|---|
| `main` | 2026-09-05 | b | 0 / 0 | default branch |
| `origin/main` | 2026-09-05 | b | 0 / 0 | tracking remote of default |
| `origin` | 2026-09-05 | b | 0 / 0 | remote head |
| `shell/standardise` | 2026-08-10 | b | 0 / 70 (main is 70 ahead) | `[inferred]` earlier shell/tooling standardisation effort — reachable via merge commit `310325a` |
| `upstream` | 2025-08-11 | 0x6rss | 507 / 38 | `[inferred]` original `matkap` fork tip; the local `main` has diverged with 507 unique commits |
| `upstream/main` | 2025-08-11 | 0x6rss | 507 / 38 | same as `upstream` |

## 14. Pull requests

- Open PRs (via `gh pr list --state open --limit 50`): `[]` (none).
- Merged PRs (via `gh pr list --state merged --limit 30`): one row returned — PR #25 "Skip previously seen message IDs and persist HTTP 400 (missing) IDs with UI toggle" by `GWSoT` (Nazar Yatsyk), merged 2025-08-11, head `main` → base default.
- Additional merge commits visible in `git log --merges`: `310325a` "Merge pull request #3 from <owner>/shell/standardise"; two "Merge branch 'main'" merges from the owner's fork; `408698f` "Merge pull request #25 from GWSoT/main".
- The `gh` search returned only PR #25 for merged state — the other numbered PR (#3) is not surfaced by `gh` (`[inferred]` — likely because the head repository or branch was deleted), but a merge commit references it.

## 15. Change activity

Most-modified files over the last 200 commits (`git log -n 200 --name-only`):

| Rank | Count | File |
|---|---|---|
| 1 | 50 | `app/workers/tasks/flow_tasks.py` |
| 2 | 36 | `app/workers/celery_app.py` |
| 3 | 26 | `app/services/bot_listener.py` |
| 4 | 22 | `app/core/config.py` |
| 5 | 20 | `app/services/broadcaster_srv.py` |
| 6 | 19 | `app/workers/tasks/scanner_tasks.py` |
| 7 | 18 | `docker-compose.yml` |
| 8 | 18 | `app/services/user_agent_srv.py` |
| 9 | 16 | `app/api/routers/monitor.py` |
| 10 | 16 | `.env.template` |
| 11 | 15 | `app/workers/tasks/validation_tasks.py` |
| 12 | 13 | `app/services/scraper_srv.py` |
| 13 | 12 | `extension/background.js` |
| 14 | 12 | `README.md` |
| 15 | 11 | `app/services/_scraper/strategies.py` |
| 16 | 10 | `app/services/scanners.py` |
| 17 | 10 | `app/core/audit.py` |
| 18 | 9 | `app/api/main.py` |
| 19 | 9 | `app/workers/tasks/audit_tasks.py` |
| 20 | 8 | `frontend/components/ChatWindow.tsx` |

Contributors (`git shortlog -sn --all`): `b` — 468 commits; `0x6rss` — 70; `Nazar Yatsyk` — 6; `GitHub Actions Sync` — 3.

Tags / releases: `git tag --sort=v:refname` returned no entries — no tags are present in this repository.

## 16. Documentation vs. code

| Doc claim | Source | Code evidence | Status |
|---|---|---|---|
| "Delivered as a Docker Compose stack" | `README.md:5` | `docker-compose.yml` defines `redis`, `api`, `worker-core`, `worker-scanners`, `worker-scrape`, `worker-validators`, `beat`, `bot`, `flower`, `frontend` | confirmed |
| "10 services" | `README.md:161` | 10 top-level entries in `services:` block of `docker-compose.yml` | confirmed |
| "13 public data sources" | `README.md:5`, `PRD.md:12` | 21 scanner service classes exist (`app/services/scanners.py` + `scanners_extension.py`); beat schedule enables 19 `scanner.scan_*` entries with 5 explicitly disabled (`fofa`, `gitlab`, `pastebin`, `replit`, `google`); the count depends on definition | unverified |
| "Deployment: Docker Compose  7 services" | `PRD.md:16` | 10 services present in compose file | contradicted |
| "worker-core ... concurrency: 4" | `PRD.md:32` | `docker-compose.yml` sets `--concurrency=8`; prod override uses `${WORKER_CORE_CONCURRENCY:-8}` | contradicted |
| "worker-scanners ... concurrency: 2" | `PRD.md:36` | `docker-compose.yml` sets `--concurrency=8` | contradicted |
| "Celery Beat  schedules 25 tasks" | `PRD.md:26` | `beat_schedule` dict in `app/workers/celery_app.py` contains ~40 entries (counted between lines 220 and 500) | contradicted |
| "383 tests (317 unit + 7 integration + 59 top-level)" | `README.md:245` | 59 test files: 46 in `tests/unit/`, 3 in `tests/integration/`, 1 in `tests/load/`, 9 top-level under `tests/` (excluding `conftest.py`) | unverified (file-level counts do not match; individual test-function count not measured in this run) |
| "Encryption key ... 44 chars" | `README.md:33` | `field_validator('ENCRYPTION_KEY')` asserts `len(v) != 44` (`app/core/config.py:165`) | confirmed |
| "MONITOR_API_KEY ... If set, all `/monitor/*` and `/health/detailed` endpoints require `X-Monitor-Key`" | `README.md:42` | `MONITOR_API_KEY: str` is declared without a default (`app/core/config.py:46`) so it is required, not "if set"; every monitor/health/scan/ingest/media/honeypot-status route depends on `require_monitor_key` | contradicted / outdated |
| "ENCRYPTION_KEY_LEGACY ... previous keys" | `README.md`, `docs/HONEYPOT.md` cross-refs | `SecurityService` uses `MultiFernet` when a legacy list is present (`app/core/security.py`) | confirmed |
| "HONEYPOT_REDIRECT_MODE=True" enables outgoing redirect | `README.md` "Honeypot Redirect Injection" | Two gates: `HONEYPOT_REDIRECT_MODE` and `HONEYPOT_REDIRECT_AUTHORIZED`. Outbound send only proceeds when both are `True` (`app/core/config.py:36-46`, `app/workers/tasks/flow_tasks.py:honeypot_redirect_one`) | contradicted (README omits the second gate) |
| `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE` default `False` | `README.md:60` | `config.py:68` default `False` | confirmed |
| "Frontend (optional): Next.js 16 + React 19" | `README.md:9` | `frontend/package.json` `next ^16.2.4`, `react 19.2.3` | confirmed |
| License = Apache-2.0 | `README.md:345` | `LICENSE:1` "Apache License" | confirmed |
| `.env.template` "MONITOR_BOT_TOKEN=123456789:replace-token,987654321:replace-token" | `.env.template` | `Settings.parse_bot_tokens` splits on `,` and validates each has `digits:secret` (`app/core/config.py:163`) | confirmed |
| README "10 services" list includes `worker-validators` and `bot`; PRD "7 services" table does not | `README.md:161`, `PRD.md:22` | code has both `worker-validators` and `bot` services | PRD outdated |
| "CANARY_EXPECTED_TEXT default TheprawnHunter-canary" | `README.md:66` | `config.py:78` default `telegramhunter-canary` | contradicted (casing and hyphenation differ) |

Capabilities present in code but not surfaced in `README.md`:
- The `/ingest/tokens` endpoint (`app/api/routers/ingest.py:212`) — plain-text/JSON token paste.
- The `/monitor/search`, `/monitor/operators`, `/monitor/broadcasts/pending`, `/monitor/broadcasts/{id}/retry`, `/monitor/topics/revoked/close`, `/monitor/engagement/lifecycle`, `/monitor/findings/*`, `/monitor/targets/export`, `/monitor/export` endpoints (only a subset appear in README).
- The full `flow.*` task family for findings/entities/engagement (`flow.produce_findings`, `flow.build_entity_graph`, `flow.route_finding_deltas`, `flow.daily_findings_digest`, `flow.weekly_finding_alerts`, `flow.source_quality_report`, `flow.attribution_graph_report`).
- The GitHub Events firehose (`firehose.poll_github_events`, `app/workers/tasks/firehose_tasks.py`) — 30 s cadence.
- The pivot fan-out family (`pivot.search_github_user`, `pivot.search_bot_username`, `pivot.search_webhook_host`).
- The multi-touch redirect follow-up tasks (`flow.honeypot_redirect_touch2`, `touch3`, `honeypot_proactive_outreach`) and the `HONEYPOT_REDIRECT_AUTHORIZED` gate.
- `PSEUDONYMIZATION_KEY`, `ALLOW_PUBLIC_STARTHUNTER`, `EXTRA_CORS_ORIGINS`, `RESCRAPE_BACKPRESSURE_THRESHOLD`, `BROADCAST_BATCH_SIZE`, `BROADCAST_RETRY_MAX_DELAY_SECONDS`, `BROADCAST_RETRY_JITTER_RATIO`, `SERPER_API_KEY`, `POSTMAN_API_KEY`, `TELETHON_PROXY_URL`, `HTTP_PROXY_URL`.
- The frontend directory `frontend/components/TelemetryAnalyticsView.tsx` and the sign-in flow at `frontend/app/signin/`.

## 17. Gaps and unknowns

- No git tags or releases exist; there is no versioning artifact in tree beyond `package.json:"version": "1.0.0"` and `frontend/package.json:"version": "0.1.0"`.
- The exact count of pytest test functions was not measured in this run because tests were not executed; only file-level counts were verified. The README claim of "383 tests" is therefore neither confirmed nor refuted from code inspection alone.
- No test files exist under `tests/` that reference `app/services/bot_manager_srv.py`, `app/workers/tasks/pivot_tasks.py`, `app/workers/tasks/firehose_tasks.py`, `app/workers/tasks/import_tasks.py`, or the `extension/` sources.
- Root-level HTML files `plan.html`, `competitive-upgrade-plan.html`, `competitive-upgrade-plan-CORRECTED.html`, and root-level Markdown files `AUDIT.md`, `AUDIT_LOG.md`, `bugfix.md`, `design.md`, `tasks.md`, `security_audit.md` are tracked but not referenced by any code path; their role is not established by the code base.
- `PRD.md` describes a 7-service, low-concurrency, 25-task deployment that no longer matches `docker-compose.yml` / `celery_app.py`. Which document is intended as canonical is not established.
- `upstream/main` is 507 commits behind and 38 commits ahead of the local `main`; the intended relationship between the two histories is not established from code.
- The frontend `discovered_credentials_public` view is described in `frontend/README.md` and referenced by `frontend/components/FindingsQueue.tsx`, but its DDL lives in `supabase/migrations/20260903000005_discovered_credentials_public_authenticated.sql` (not read in this run); the exact projection was not fully verified against `frontend` reads.
- `docker-compose.yml` marks the four named volumes as `external: true` with the legacy `telegramhunter_*` prefix; if the volumes are not pre-created, `docker compose up` fails. The README documents this only in the volume-name block near the end.
- The `.claude/scheduled_tasks.lock` file is tracked but its consumer is not identifiable from the codebase.
- `.playwright-mcp/page-2026-04-24T02-45-32-072Z.yml` is a captured browser session; its intended re-use path is not established.
