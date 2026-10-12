# Repository Map

## Snapshot

This map reflects the Step 10 code at commit `bbda253364f1044838700fdc2da205866be5fc4a`; Step 11 refreshes the structural documentation. The snapshot contains 460 tracked files, 253 Python files, 65 test files, and 28 chronological SQL migrations.

The repository contains a Docker Compose application: FastAPI, Celery workers and scheduler, a Telegram admin bot, Redis, Flower, a Next.js dashboard, and Cloudflare Tunnel tooling. PostgreSQL is accessed through Supabase. The active migration stream is `supabase/migrations/`; `database/init.sql` and `database/rls_policies.sql` are reference snapshots.

## Runtime services and entry points

The Compose service names are `redis`, `api`, `worker`, `beat`, `bot`, `flower`, `frontend`, and `cloudflared`. They and their volumes are stable deployment interfaces.

| Service | Entry point | Role |
|---|---|---|
| `api` | `app.api.main:app` | FastAPI routes; Gunicorn listens on container port 8001. |
| `worker` | `app.workers.celery_app` | Celery task queues `celery`, `scanners`, `scrape`, and `validation`. |
| `beat` | `app.workers.celery_app` | Periodic task scheduler and persistent beat schedule. |
| `bot` | `python -m app.services.bot_listener` | Telegram admin commands and login conversation. |
| `redis` | Redis 7 | Celery broker/result backend, locks, caches, and application deduplication. |
| `flower` | `app.workers.flower_app` | Celery monitoring UI. |
| `frontend` | Next.js server | Analyst dashboard. |
| `cloudflared` | Cloudflare Tunnel | Optional inbound tunnel service. |

The checked-in host API default is `API_PORT=8011`, mapped to container port 8001. The migration audit sets host port **3410** as a pre-deployment acceptance gate. This checkout has no listener on 3410; preserve the port contract and resolve the deployment environment/proxy mapping before any rollout. No port or service names are changed by this map update.

## Application structure

```text
app/
  api/
    main.py
    routers/
      health.py, honeypot.py, ingest.py, media.py, scan.py
      monitor.py                  # public /monitor router facade
      _monitor/                   # resource routers and shared dependencies
  core/                            # config, auth, database, security, Redis, metrics, audit
  domain/                          # shared token/media/domain primitives
  infrastructure/
    database.py                    # shared async execution boundary
    scanners/
      common.py
      providers/<provider>.py     # scanner service implementations
  services/
    scanners.py                    # public scanner service facade
    scanners_extension.py          # extension-provider facade
    bot_listener.py                # stable import and `python -m` facade
    user_agent_srv.py              # user-agent service facade
    _bot_listener/                 # command, login, update, runtime implementations
    _scraper/                      # scrape strategies, lifecycle, result classification
    _user_agent/                   # session lifecycle and group/archive operations
    ...                            # broadcaster, scraper, entities, findings, engagement
  workers/
    celery_app.py                  # Celery app, task routes, beat schedule
    tasks/
      flow_tasks.py                # stable task/import facade
      flows/                       # domain-split flow task implementations
      scanner_tasks.py             # stable scanner task facade
      scanners/
        common.py
        providers/<provider>.py   # provider-specific Celery tasks
      audit_tasks.py, validation_tasks.py, pivot_tasks.py, ...
```

The monitor router keeps the `/monitor` prefix and its existing auth dependency while including resource routers from `_monitor/`. Celery task modules retain explicit task names; `flow_tasks.py` and `scanner_tasks.py` expose the established imports while implementations live in `flows/` and `scanners/providers/`. The service facades similarly preserve established imports and the bot listener module entry point.

## Naming conventions used in Steps 1–10

- Use lowercase `snake_case` for Python modules and provider filenames (`github.py`, `commoncrawl.py`, `telegram_search.py`).
- Group implementation modules by domain in plural packages such as `flows/` and `providers/`; keep each module scoped to one task domain, API resource, or provider.
- Prefix implementation-only packages with `_` where a stable public facade remains (`_monitor/`, `_bot_listener/`, `_scraper/`, `_user_agent/`).
- Keep legacy public module paths and explicit Celery task names stable through facades. Do not mass-rename established `_srv` service modules or bare-name modules.
- Classify operator scripts by purpose: `scripts/dev/`, `scripts/deploy/`, and `scripts/ops/`; place manual recovery and inspection utilities in `scripts/ops/one_off/`. Root-level script paths retained for compatibility should forward to the categorized implementation.
- Keep versioned schema changes in timestamped files under `supabase/migrations/`; reference snapshots in `database/` are not an alternate migration stream.
- Name Python test modules `test_<area>_<behavior>.py` under `tests/` and its `unit/`, `integration/`, or `load/` subdirectories.

## Operational scripts

```text
scripts/
  dev/                           # local setup, test fixture, FOFA helper
  deploy/                        # bootstrap, startup, release and validation tooling
  ops/                           # operational reports, probes, drift checks and backfill CLI
    one_off/                     # manual credential/media recovery and inspection tools
  <legacy names>                 # forwarding entry points and compatibility config files
```

Historical media recovery uses one CLI: `python scripts/ops/backfill_media.py {all,since,telethon}`. Every mode is dry-run by default; `--apply` is explicit. The deployment import/runtime validator is `scripts/deploy/validate_deployment.py`. Legacy script entry points forward to the categorized implementations; top-level scheduler XML and logrotate files remain available for compatibility.

## Main execution paths

### Scan, validate, and save

1. Celery Beat schedules `scanner.*` tasks through `app.workers.celery_app`.
2. `app.workers.tasks.scanner_tasks` preserves scanner task imports; provider task bodies live in `app/workers/tasks/scanners/providers/`.
3. Provider service implementations live in `app/infrastructure/scanners/providers/` and are re-exported through `app.services.scanners` or `app.services.scanners_extension`.
4. Valid candidates flow to `validation.validate_token`, then successful credentials can enqueue pivot and enrichment tasks.

### Enrich, scrape, and broadcast

1. `flow.enrich_credential` and related tasks are implemented in `app/workers/tasks/flows/` and exported by the `flow_tasks.py` facade.
2. Scrape lifecycle and strategies are implemented by `app/services/_scraper/` behind `app.services.scraper_srv`.
3. Messages are stored in `exfiltrated_messages`; `(credential_id, telegram_msg_id)` is the unique message identity.
4. `flow.broadcast_pending` claims pending messages and calls `BroadcasterService`; broadcast state and retries are stored in the database.

### Honeypot receive and redirect

1. `/honeypot/receive/{credential_id}` is served by `app/api/routers/honeypot.py` and is gated by honeypot mode, secret token, and credential allowlist.
2. Updates are persisted and eligible work is dispatched to the existing redirect task family.
3. Outbound redirect behavior has both `HONEYPOT_REDIRECT_MODE` and `HONEYPOT_REDIRECT_AUTHORIZED` gates.

### Telegram admin bot

`app.services.bot_listener` remains the runnable module and compatibility facade. Its command, login, update, and runtime code is under `app/services/_bot_listener/`. `/backfill` defaults to dry-run; explicit `--apply` is required to requeue rows.

## Stable interfaces

The refactors preserve:

- HTTP route paths and prefixes, including `/monitor` and `/honeypot/receive/{credential_id}`.
- Celery task names and queue routes.
- Compose service names, volume names, session paths, and container port 8001.
- Established import paths and `python -m app.services.bot_listener` through facades.

The monitor routes and API auth wiring are assembled in `app/api/routers/monitor.py`; task registration and queue routing remain in `app/workers/celery_app.py`.

## Data and migrations

Supabase Postgres stores credentials, messages, monitor data, telemetry, audit events, honeypot updates, findings, and related workflow state. Redis stores Celery state and runtime coordination, including the Step 10 general-backfill dedup set keyed by credential/message identity. That Redis marker is an application fallback; a dedicated `backfilled_at` database column remains owed through the migration-authority process.

`supabase/migrations/` is the only schema-change stream. `database/init.sql` and `database/rls_policies.sql` are reference snapshots. Do not apply snapshots as migrations.

## Tests and tooling

Python tests live under `tests/` and use pytest. `tests/conftest.py` supplies synthetic settings before importing the API app. Frontend tests use Vitest and Testing Library. CI workflow definitions are under `.github/workflows/`.

The root `package.json` and lockfile support the FOFA helper. Frontend dependencies and tests are managed separately under `frontend/`.
