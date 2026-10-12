# Production Runbook

This Compose deployment relies on Redis, Supabase, Telegram Bot API, Celery
workers, and a Next.js frontend. Run these checks before deployment, after
restart, and during soak tests.

## One-Time Host Setup

Create the external volumes used by `docker-compose.yml` before the first run.
These names intentionally preserve the existing production data volumes and
avoid Compose warnings about legacy project labels.

```powershell
docker volume create telegramhunter_redis_data
docker volume create telegramhunter_sessions
docker volume create telegramhunter_imports
docker volume create telegramhunter_beat_schedule
```

If you intentionally migrate to clean `theprawnhunter_*` volumes, copy data
first, then override `REDIS_VOLUME_NAME`, `SESSIONS_VOLUME_NAME`,
`IMPORTS_VOLUME_NAME`, and `BEAT_SCHEDULE_VOLUME_NAME`.

## Schema Management

Use one approved admin SQL path on this machine:

- Set `DATABASE_URL` to a Supabase Postgres connection string, then run:
  `python scripts/ops/schema_drift_check.py`
- Or authenticate/link the Supabase CLI and run the same SQL through the CLI.
- Or use an authenticated Supabase MCP SQL tool.

Do not use REST service-role row access for schema management. Before applying
production DDL, run:

```powershell
supabase --version
supabase db --help
supabase db advisors --help
supabase db advisors --linked
python scripts/ops/schema_drift_check.py
```

The drift check fails on missing required tables, columns, or indexes and
reports optional broadcast/scrape hardening columns separately.

## Release Gate

Run the standard gate before deployment:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/deploy/release_gate.ps1 -SkipLive
```

When the stack is already running and `MONITOR_API_KEY` is configured:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/deploy/release_gate.ps1
```

The gate runs focused runtime tests, `docker compose config --quiet`, Docker
build, and the live operational API report unless skipped.

## Restart

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose ps
python scripts/ops/ops_report.py
python scripts/ops/schema_drift_check.py
```

Post-restart log scan:

```powershell
docker compose logs --since 15m | Select-String -Pattern `
  "Traceback","Task was destroyed","PGRST204","does not exist","flood control","RetryAfter"
```

## Operator Checks

Queues, canary, and recent failure reasons:

```powershell
python scripts/ops/ops_report.py
```

Pending broadcast triage:

```powershell
curl -H "X-Monitor-Key: $env:MONITOR_API_KEY" `
  "http://127.0.0.1:8011/monitor/broadcasts/pending?failed_only=true"
```

Retry one failed unbroadcasted message:

```powershell
curl -X POST -H "X-Monitor-Key: $env:MONITOR_API_KEY" `
  "http://127.0.0.1:8011/monitor/broadcasts/<message_id>/retry"
```

Retry a batch from Celery:

```powershell
docker exec theprawnhunter_worker-core celery -A app.workers.celery_app call `
  flow.retry_failed_broadcasts --kwargs='{"limit":50}'
```

Dry-run closing topics for revoked credentials:

```powershell
curl -X POST -H "X-Monitor-Key: $env:MONITOR_API_KEY" `
  "http://127.0.0.1:8011/monitor/topics/revoked/close?dry_run=true&limit=50"
```

Close a batch after reviewing the dry-run:

```powershell
curl -X POST -H "X-Monitor-Key: $env:MONITOR_API_KEY" `
  "http://127.0.0.1:8011/monitor/topics/revoked/close?dry_run=false&limit=50"
```

Automatic revoked-topic closure:

```powershell
# Enabled by default. Beat queues flow.close_revoked_topics every 5 minutes.
AUTO_CLOSE_REVOKED_TOPICS=True
REVOKED_TOPIC_CLOSE_INTERVAL_MINUTES=5
REVOKED_TOPIC_CLOSE_BATCH_SIZE=25
REVOKED_TOPIC_CLOSE_TIMEOUT_SECONDS=10
```

When enabled, credential revocation paths also queue a one-record close check
for the affected credential. Closing a Telegram forum topic locks it but keeps
the topic history.

Telegram behavior probe:

```powershell
python scripts/ops/telegram_behavior_probe.py
python scripts/ops/telegram_behavior_probe.py --matrix docs/telegram_probe_matrix.example.json
```

Use only non-production bots/chats for the matrix. Webhook deletion remains
disabled unless a case explicitly sets `allow_delete_webhook=true`.

## Soak Test

Run overnight with API, bot, beat, queue monitor, broadcast worker, scrape
worker, scanner worker, and validation worker active. Capture:

- Queue length and oldest job age for `celery`, `scrape`, `scanners`, `validation`
- Canary success rate and last successful age
- Broadcast failure counts by reason
- Scrape terminal reason counts
- Telegram flood-control warnings
- Container restarts and memory pressure

Use this loop for periodic snapshots:

```powershell
while ($true) {
  Get-Date
  docker compose ps
  python scripts/ops/ops_report.py
  Start-Sleep -Seconds 900
}
```

## Rollback

```powershell
git log --oneline -5
git revert <bad_commit_sha>
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
python scripts/ops/ops_report.py
```

If schema changes were applied, restore from the Supabase backup/snapshot or
apply an explicit rollback migration after advisors and drift checks.
