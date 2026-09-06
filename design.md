# design.md — Implementation Plan for 02_EXECUTE Cycle

Source: `AUDIT.md` (commit `3a033cfe06123627c37cbfb03662a9a543c61ebf`).
Selection: `fix all` — 69 findings (see `bugfix.md` for status and dedup).
Branch: `remediation/2026-09-06`.

Principle: **minimum viable change**. Preserve intent, style, and architecture. No opportunistic refactoring. No fixes outside the selection.

---

## 1. Executive shape of this cycle

45 discrete atomic tasks after dedup (see `bugfix.md` summary). Grouped into 5 execution sessions:

| Session | Scope | Approx tasks | Approx effort | Requires operator step |
|---|---|---|---|---|
| A — P0 security + data integrity | SEC-001, DATA-001, INTR-001, SEC-002, INTR-002/CONC-002, LOGIC-004 | 8 | 4-6 h | Yes: apply SQL migrations, run re-encrypt script |
| B — P1 reliability | DATA-002, DATA-003/REL-003, INTR-003, INTR-004, INTR-005, LOGIC-001, LOGIC-002/DATA-004, CONC-001, REL-002 | 10 | 4-6 h | No |
| C — P2 hardening | SEC-004, SEC-005, LOGIC-003, LOGIC-005, DATA-005, DATA-006, CONC-003, CONC-004, PERF-001, PERF-004, PERF-006, REL-004, REL-005 | 13 | 4-6 h | Yes: apply CONC-003 migration if UNIQUE(message_id) missing |
| D — Structural moves + drift | STRUCT-001, STRUCT-002, STRUCT-003, STRUCT-007, DRIFT-005, DRIFT-007, FS-001, FS-002, FS-003, DEAD-001, DEAD-002, DEAD-006 | 12 | 2-3 h | No |
| E — P3 cleanup | SEC-006, LOGIC-006, LOGIC-007, REL-006, DEAD-007, FE-002 | 6 | 1-2 h | No |
| Runbook | REL-001 deploy runbook (`docs/deployment/rebuild.md`) | 1 | 30 min | Yes: entire runbook is operator-driven |

**Explicit deferrals**: PERF-002 (broadcast parallelisation), PERF-003 (MTProto pooling), DEAD-005 (DeepSource/Sourcery activity check), DEAD-008 (142 broad-except sites) — reasoning in each `bugfix.md` entry.

**Explicit handoffs**: PRD.md rewrite (DRIFT-003/004/006) and README refresh belong in `03_DOCUMENT` per the pipeline design. This cycle notes them in `bugfix.md` but does not touch those docs.

---

## 2. Architecture changes

### 2.1 `broadcast_message_id` column on `exfiltrated_messages` (INTR-001)

The single behavioral schema change. Every other change is either config, code, or file relocation.

**Why unavoidable**: broadcast idempotency requires persisting the Telegram-returned message id. Any in-memory scheme dies with the worker. The current claim-only design tolerates duplicates on interruption; recording the outbound message-id is the minimum change that closes that window.

**Alternative considered and rejected**: computing a broadcast fingerprint and searching Telegram history on retry — costs one MTProto round-trip per retry, fragile against topic recreation. Rejected on complexity.

### 2.2 `broadcast_status` column on `exfiltrated_messages` (DATA-003 / REL-003)

Adds a `TEXT CHECK (...)` column with values `pending`, `sent`, `permanent_failed`, `revoked`. When `broadcast_attempts >= MAX_BROADCAST_ATTEMPTS`, the row transitions to `permanent_failed` and `is_broadcasted=true` (so it exits the retry pool) — but with the new column showing the true state.

**Why unavoidable**: without a permanent-failed classification the retry pool grows indefinitely. Using `is_broadcasted=true` alone would falsely claim success in stats and hide the failure.

### 2.3 `is_failure` / `failure_reason` columns on `media_hashes` (INTR-005)

Replaces the `sha256='__failed__<id>'` sentinel string with structured columns.

**Why unavoidable**: sentinel scheme is fragile against message-id shape change and pollutes the `sha256` index.

### 2.4 `honeypot_updates.redirected_at` semantic change (CONC-002 / INTR-002)

`redirected_at` gains an intermediate state `'pending'` for claim-before-dispatch. Value is a UTC ISO string; the sweep uses:

```
UPDATE honeypot_updates
   SET redirected_at = to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS')
 WHERE id = $1 AND redirected_at IS NULL
RETURNING id;
```

Post-send, the value updates to the true timestamp; on failure it flips back to `NULL`.

**Why unavoidable**: same as INTR-001 — closes the duplicate-DM window.

Everything else in this cycle is content within existing shapes.

---

## 3. Data model changes and migrations

All migrations are additive, forward-only, and idempotent (`IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`). None drop columns, tables, or constraints. **I write the migration files; the operator applies them via `supabase db push` or the Supabase SQL editor.**

| Migration file | What it does | Forward | Rollback |
|---|---|---|---|
| `20260906000002_deprecate_extension_direct_write.sql` | Drops "Extension Insert" / "Extension Update" policies on `discovered_credentials`; adds `CHECK (bot_token LIKE 'gAAAA%')` constraint (deferred via `NOT VALID` initially so existing rows aren't blocked pre-migration script) | New RLS state | Recreate the two policies + `ALTER TABLE ... DROP CONSTRAINT` |
| `20260906000003_broadcast_reliability_ext.sql` | `ADD COLUMN IF NOT EXISTS broadcast_message_id BIGINT`, `ADD COLUMN IF NOT EXISTS broadcast_status TEXT CHECK (broadcast_status IN ('pending','sent','permanent_failed','revoked'))` on `exfiltrated_messages`; default `broadcast_status='pending'` for FALSE, `'sent'` for TRUE (backfill via `UPDATE`) | Column additions + backfill | `ALTER TABLE ... DROP COLUMN` (safe — additive) |
| `20260906000004_media_hashes_failure_flag.sql` | Adds `is_failure BOOLEAN DEFAULT FALSE, failure_reason TEXT` on `media_hashes`; `UPDATE ... SET is_failure=TRUE WHERE sha256 LIKE '__failed__%'` migration; also `UNIQUE(message_id)` (CONC-003) if absent | Additive + backfill + unique index | Drop columns; drop unique index |
| `20260906000005_audit_logs_composite_idx.sql` | `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_event_type_timestamp ON audit_logs (event_type, timestamp DESC)` (PERF-001) | New index | `DROP INDEX` |
| `20260906000006_verify_pending_migrations.sql` | No DDL — a `SELECT 1` inside a `DO $$ ... $$` block that raises if any of `findings`, `finding_evidence`, `engagement_events`, `honeypot_redirect_log` tables are missing. Used to verify DATA-001 fix applied. | No DDL | n/a |

**Rollback protocol**: for each migration, a paired file `20260906000<n>_<name>_rollback.sql` is not shipped by default (Supabase doesn't ship reverse migrations) — instead, the rollback SQL is documented as a `-- ROLLBACK` block at the bottom of the forward file, so an operator can copy-paste to reverse. This matches the convention of the existing `supabase/migrations/`.

**Migration ordering**:
1. Apply `20260906000006` first to verify DATA-001 is resolved.
2. Then `20260906000003` and `20260906000004` (schema shape for INTR-001 / DATA-003 / INTR-005 / CONC-003).
3. Then `20260906000005` (index — CONCURRENTLY, no lock).
4. Run re-encrypt script (SEC-001) — see §5.
5. Finally `20260906000002` (RLS + constraint change) — after all plaintext tokens are Fernet-encrypted, otherwise the constraint validation fails.

**I will NOT run any of these migrations against the live database.** Only the operator does. The runbook lives in `docs/deployment/rebuild.md`.

---

## 4. Interface / contract changes

| Change | Call sites | Effect |
|---|---|---|
| `broadcaster_srv.send_message(...)` returns `int \| None` (Telegram message-id) instead of nothing | `flow_tasks.py:_broadcast_logic` only | Adds one return-value handling step; existing send_message callers in bot_listener and canary do NOT need message-id (they discard it). |
| `flow_tasks.honeypot_redirect_one_logic` gains a claim-check preamble | Beat sweep task; direct callers must pass through sweep first | Direct invocation (`docker exec ... celery call flow.honeypot_redirect_one`) will need a valid `update_id` claim — small operator caveat, doc'd in `docs/deployment/rebuild.md`. |
| `AuditLogger.log(..., details=...)` payloads over 8 KB are truncated | Every caller | No behavioral change for well-formed callers (payload sizes are small). Callers relying on exact evidence in DB should not — evidence is logged full to stdout. |
| `broadcast_pending` returns `{"status": "disabled_run"}` when `ENABLE_RAW_MESSAGE_BROADCAST=False`; increments metrics counter | Beat only | Existing `str` return value stays as a fallback for older code; new callers should use the dict shape. |
| `/honeypot/status` response drops `secret_configured`, `allowlist_mode`, `allowlist_size` | Any dashboard / monitoring client | If a user tool depends on those fields, they will get `KeyError`. Mitigation: keep them in the response but with `secret_configured` collapsed to `bool` only (already bool). Actually re-reviewing: `mode_enabled` and `receiver_url_configured` stay; `allowlist_mode` and `allowlist_size` become `allowlist_configured: bool`. Test in Session A. |

No breaking changes to public REST paths.

---

## 5. Re-encryption script (SEC-001)

`scripts/encrypt_plaintext_tokens.py` — a new operator-run one-off:

```
Usage: python scripts/encrypt_plaintext_tokens.py [--dry-run] [--batch-size N]
```

Design:
1. Load `SecurityService` from `app.core.security` — same `ENCRYPTION_KEY` as the app.
2. Page through `SELECT id, bot_token FROM discovered_credentials WHERE bot_token NOT LIKE 'gAAAA%' ORDER BY id LIMIT N OFFSET 0`.
3. For each row: validate token shape via `_is_valid_token`; skip and log rejections. Encrypt via `security.encrypt(bot_token)`. Persist with `UPDATE ... SET bot_token=$enc WHERE id=$id AND bot_token=$old` (optimistic concurrency — refuse to overwrite if another writer changed it).
4. Emit `AuditLogger.log("token.migrated_to_ciphertext", credential_id=id)` for every success.
5. Print summary: `re-encrypted N / skipped M (invalid token shape) / conflicts K`.
6. Exit non-zero if any row was skipped as invalid — operator triages.

`--dry-run` prints the intended action per row without writing.

**Why a script, not a task**: One-off migration event. Long-running tasks in Celery would block a queue and are harder to monitor. A CLI keeps the operator in the loop.

**Idempotent**: safe to re-run. Rows already encrypted are excluded by the SQL filter.

---

## 6. New dependencies

None. Every fix uses stdlib, existing deps (`httpx`, `supabase`, `cryptography`, `python-telegram-bot`), or a new migration file. No `pip install`, no `npm install`.

---

## 7. Compatibility check on existing pinned versions

Not touching versions in this cycle. Verified only that:
- `cryptography==46.0.7` supports `MultiFernet` (yes — since 1.6).
- `supabase==2.28.3` supports `.rpc(...)`, `.upsert(..., on_conflict=..., ignore_duplicates=True)` (yes — API stable since 2.x).
- `httpx[socks]==0.28.1` supports `AsyncClient(verify=False, timeout=...)` (yes — trivial).
- `celery==5.6.3` supports `worker_ready`, `worker_shutdown`, `task_failure`, `before_task_publish`, `task_prerun` signals (yes — stable API).

No lookups needed.

---

## 8. Blast radius per change

| Change | Blast radius |
|---|---|
| SEC-001 re-encrypt script | Reads and writes only `discovered_credentials.bot_token`; excludes already-encrypted rows. Zero risk to `exfiltrated_messages`, `telegram_accounts`, workers. Operator runs in dry-run first. |
| SEC-001 RLS policy removal | Anon `INSERT/UPDATE` on `discovered_credentials` becomes forbidden. Only extension direct-write path is affected; extension is already being routed to `/ingest/extension/credentials` in the same session. |
| INTR-001 idempotent broadcast | Adds column + one send-time write + one retry-check read. Purely additive to existing flow. |
| DATA-003 permanent-failed classification | Rows with `broadcast_attempts >= 8` transition to `is_broadcasted=true` (exit retry pool) with `broadcast_status='permanent_failed'`. `/monitor/stats` counts these as broadcasted (matches current behaviour), but a new `/monitor/broadcasts/permanent_failed` endpoint would surface them. Not shipped this cycle. |
| DATA-002 audit prune scope | `SCRAPE_STRATEGY_ATTEMPT` events no longer land in `audit_logs`. `/health/operational` reads these from Redis / other sources still. Verified via `flow_tasks.py` — Redis has the counters. |
| PERF-004 stats cache to Redis | Adds one Redis roundtrip per cache miss; saves 4× Supabase hit. Neutral or better latency. |
| STRUCT moves | Every path referenced in tests, imports, or documentation needs verification. Test suite run confirms nothing regressed. |
| `verify=False` gating for webhook probes (SEC-004) | Default keeps current behaviour (verify=False). If operator sets `TLS_VERIFY_WEBHOOK_PROBES=True`, probes now enforce TLS — some previously-observable C2 hosts on self-signed certs will fail probes. Documented in `.env.template`. |
| Regex tightening (LOGIC-007) | Audit logger will redact fewer non-token strings. No functional change; may improve readability. |
| Extension routing through API | Extension must have `apiUrl` configured. If unset, fall back to legacy direct-write path — but that path is now RLS-denied after SEC-001. Extension will error visibly (not silently fail). Documented. |

---

## 9. Rollback plan

**Per change:**
- Code changes: `git revert <commit>`, per-commit granularity (one commit = one fix ID or a merged pair).
- Migrations: each file has a `-- ROLLBACK` block at the bottom with the reverse DDL.
- Re-encrypt script: idempotent — re-running does nothing (excludes already-encrypted rows). To undo: no reasonable path. If a specific row was corrupted, restore from a Supabase snapshot for that row only.

**Full-cycle rollback:**
- `git checkout main` (leaves this branch behind).
- Rollback migrations in reverse order using each `-- ROLLBACK` block.
- Redeploy old image (`docker inspect ... --format {{.Config.Image}}` before rebuild = the image tag to roll back to).

**Test suite is the tripwire.** Session A ends with a green `pytest` + `ruff` + `frontend npm test` before Session B starts. Same for each session.

---

## 10. What I am NOT changing and why

| Not changing | Why |
|---|---|
| `broadcast_pending`'s serial-send loop (PERF-002) | Requires latency benchmark before/after; parallelisation across topics can trigger Telegram flood-wait on per-chat basis. Deferred with a design note. |
| MTProto client pooling in broadcaster (PERF-003) | Deferred with PERF-002; single-issue benchmark needed. |
| 142 broad-except sites (DEAD-008) | Per-site review; too large for one cycle. A per-site inventory is added to `docs/history/broad_except_review.md` so the next cycle can start from data. |
| `.deepsource.toml`, `.sourcery.yml` (DEAD-005) | Cannot verify subscription from code alone. Operator confirmation required. |
| `.kiro/specs/*` | Legitimate Kiro spec content; leaving under `.kiro/` is convention. |
| Renaming `.env.template` → `.env.example` | Kiro convention across Bryan's repos is `.env.template`; consistency wins. |
| Migration file `20260906000001_dashboard_operator_authorization.sql` | Already present in `supabase/migrations/` — will just be verified applied. |
| `_scraper/`, `_scanner/` prefix (STRUCT-005/006) | Verified — intentional private packages. Marked invalid in `bugfix.md`. |
| `PRD.md`, `README.md` factual rewrites | Handled by `03_DOCUMENT` per pipeline design. |
| `AUDIT.md` | Never modified — audit report is a fixed snapshot for this cycle. |
| Any file under `.env`, `*.session`, `*.pem`, `*.key` | Non-negotiable safety rules. |

---

## 11. Verification strategy

For each session:
1. Run `ruff check app/` — pre-existing baseline is 0 issues; any new issue is a blocker.
2. Run `pytest tests/unit tests/integration tests/test_*.py -q` (unit + integration + top-level) — pre-existing baseline unknown; will capture at start of Session A. Any new failure is a blocker.
3. Run `cd frontend && npm test` — Vitest + typecheck.
4. Per-task acceptance criterion from `tasks.md`.
5. For interruption-safety fixes (INTR-*), a manual kill-worker test where practical (INTR-001 broadcast, INTR-003 CSV import).
6. `git log --oneline main..HEAD` at session-end to confirm commits are atomic and Conventional-Commit shaped.

**No live-Docker restart in verification.** The verify runs use the running stack for LIVE reads only; code changes are validated against `pytest` locally. Deploy is a separate operator step (REL-001 runbook).

---

## 12. Session-by-session file changes

### Session A (P0)
- **new** `scripts/encrypt_plaintext_tokens.py`
- **new** `supabase/migrations/20260906000002_deprecate_extension_direct_write.sql`
- **new** `supabase/migrations/20260906000003_broadcast_reliability_ext.sql`
- **new** `supabase/migrations/20260906000006_verify_pending_migrations.sql`
- **mod** `app/api/routers/honeypot.py` (SEC-002 + LOGIC-004)
- **mod** `app/workers/tasks/flow_tasks.py` (INTR-001 send-tracking, INTR-002 + CONC-002 claim-before-dispatch, LOGIC-005 reason strings)
- **mod** `app/services/broadcaster_srv.py` (INTR-001 return message-id)
- **mod** `extension/background.js` (SEC-001 route through API)
- **new** `docs/deployment/rebuild.md` (REL-001 runbook — pointer only, actual deploy is operator's)

### Session B (P1)
- **new** `supabase/migrations/20260906000004_media_hashes_failure_flag.sql`
- **new** `supabase/migrations/20260906000005_audit_logs_composite_idx.sql`
- **mod** `app/core/audit.py` (DATA-002 scope, DATA-006 cap, LOGIC-007 regex)
- **mod** `app/workers/tasks/flow_tasks.py` (INTR-005, LOGIC-002/DATA-004, CONC-001 log, LOGIC-003 metric, LOGIC-001 canary_findings_check)
- **mod** `app/workers/tasks/import_tasks.py` (INTR-003 breadcrumb)
- **mod** `docker-compose.yml` (REL-002 healthchecks)

### Session C (P2)
- **mod** `app/core/config.py` (SEC-004 `TLS_VERIFY_WEBHOOK_PROBES`, DEAD-001/002 delete SERPER/CENSYS/HYBRID)
- **mod** `app/api/routers/honeypot.py` (SEC-005 body-size cap)
- **mod** `app/api/routers/monitor.py` (PERF-004 Redis stats cache)
- **mod** `app/services/scanners.py`, `app/services/scanners_extension.py` (PERF-006 shared client; comment on `verify=False`)
- **mod** `extension/content.js`, `extension/background.js` (DATA-005 regex alignment — only if drift found)
- **mod** `README.md` (REL-004 fresh-deploy prerequisite; STRUCT-002 gitignore clarification)
- **mv** `frontend/vite.config.ts` → `frontend/vitest.config.ts` (REL-005 / STRUCT-004)

### Session D (structural)
- **mv** `AUDIT_LOG.md` → `docs/history/AUDIT_LOG.md`
- **mv** `security_audit.md` → `docs/history/security_audit.md`
- **mv** `PRD.md` → `docs/PRD.md` (moved; refresh handoff to 03_DOCUMENT)
- **mv** `SUPABASE_KEEPALIVE_SETUP.md` → rewrite → `docs/SUPABASE_KEEPALIVE.md`
- **mv** `plan.html`, `competitive-upgrade-plan.html`, `competitive-upgrade-plan-CORRECTED.html` → `docs/history/`
- **mv** `database/migrations/*.sql` (8 files) → `docs/history/legacy_migrations/`
- **mod** `.gitignore` (FS-001, FS-002, STRUCT-002)
- `git rm --cached .claude/scheduled_tasks.lock` (FS-001)
- `git rm --cached .claude/settings.local.json` (FS-002)
- `git rm --cached .playwright-mcp/*.yml` (FS-002 / DEAD-006)

### Session E (P3)
- **mod** `app/services/bot_listener.py` (SEC-006 hmac compare_digest)
- **mod** `app/api/routers/ingest.py` (LOGIC-006 reorder own-bot check)
- **mod** `app/core/audit.py` (LOGIC-007 — merged into Session B)
- **mod** `app/workers/celery_app.py` (REL-006 schedule digest at startup)
- **mv** `retry_with_backoff` from `app/services/scanners.py` → `app/utils/http_client.py` (DEAD-007)
- **mod** `.github/workflows/ci.yml` (FE-002 anon-key smoke test)
- **new** `docs/history/broad_except_review.md` (DEAD-008 inventory only; the fixes are deferred)

### Runbook
- **new** `docs/deployment/rebuild.md` — final operator playbook: apply migrations in order, run re-encrypt script, `docker compose build --no-cache` + `up -d`, Vercel redeploy, post-deploy verification checklist.

---

## 13. Test suite baseline

Before Session A begins, I will capture:
- `pytest tests/unit tests/integration -q` — exit code and count of passing/failing.
- `ruff check app/ --output-format=concise` — number of issues.
- `cd frontend && npm test` — pass/fail.

If baseline has pre-existing failures unrelated to this cycle, they are **not** unblocked here — they get logged as `NEW-###` in `bugfix.md` and reported at closeout.

---

## 14. Communication to operator

At each session boundary (A → B → C → D → E), I will:
- Update `bugfix.md`, `tasks.md`, `execute_state.json`.
- Post a `STATE:` line and a concise `SESSION-X CLOSED` summary.
- Wait for approval before starting the next session (per prompt: no unbounded auto-run).

At CLOSEOUT (Phase 4), I hand over:
- Summary table (finding × commit).
- Every migration file staged for operator apply.
- The `docs/deployment/rebuild.md` runbook.
- The residual-risk statement (fixes I could not fully verify without operator action).
