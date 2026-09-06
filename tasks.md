# tasks.md — Atomic Work List for 02_EXECUTE Cycle

Source: `AUDIT.md`, `bugfix.md`, `design.md`. Branch: `remediation/2026-09-06`.

Ordering follows prompt rules: P0 security + data integrity → P1 reliability → interruption safety → structural moves LAST among behavioral work → P2 quality → P3 cleanup → dead-code deletion absolutely last.

Tasks are grouped by execution session (A → E). Sessions are separated by an approval gate — I do NOT auto-continue.

---

## Session A — P0 Security & Data Integrity

- [ ] **T01 — Capture pre-execution baseline**
      Finding: none (protocol)
      Files: create `docs/history/pre_02execute_20260906/baseline.txt`
      Change: run `ruff check app/ --output-format=concise` and `pytest -q --tb=no` and `cd frontend && npm test`; save output.
      Acceptance: `baseline.txt` exists, records the three commands' exit codes and pass/fail counts.
      Rollback: `rm baseline.txt`.

- [ ] **T02 — Write migration `20260906000006_verify_pending_migrations.sql`**
      Finding: DATA-001
      Files: `supabase/migrations/20260906000006_verify_pending_migrations.sql`
      Change: file with a `DO $$ ... RAISE EXCEPTION IF NOT EXISTS ...` block for `findings`, `finding_evidence`, `engagement_events`, `honeypot_redirect_log`.
      Acceptance: file exists, `pytest` unaffected (schema not applied yet — file is inert until operator runs it).
      Rollback: `git rm supabase/migrations/20260906000006_verify_pending_migrations.sql`.

- [ ] **T03 — Write migration `20260906000003_broadcast_reliability_ext.sql`**
      Finding: INTR-001, DATA-003, REL-003
      Files: `supabase/migrations/20260906000003_broadcast_reliability_ext.sql`
      Change: `ADD COLUMN IF NOT EXISTS broadcast_message_id BIGINT`, `ADD COLUMN IF NOT EXISTS broadcast_status TEXT CHECK (...)` on `exfiltrated_messages`; backfill `broadcast_status='sent' WHERE is_broadcasted=true`, else `'pending'`. Add `-- ROLLBACK` block.
      Acceptance: file exists, parses as SQL, `pytest` unaffected.
      Rollback: `git rm` the file.

- [~] **T04 — SKIPPED (operator accepts plaintext)**
      Finding: SEC-001 → Deferred
      Change: not applying `20260906000002_deprecate_extension_direct_write.sql`. Extension direct-write path stays open.
      Acceptance: n/a — deferred.
      Rollback: n/a.

- [~] **T05 — SKIPPED (operator accepts plaintext)**
      Finding: SEC-001 → Deferred
      Change: not authoring `scripts/encrypt_plaintext_tokens.py`. Historical 478 plaintext tokens remain as-is.
      Acceptance: n/a.
      Rollback: n/a.

- [ ] **T06 — Consolidate honeypot router auth + trim status payload**
      Finding: SEC-002, LOGIC-004
      Files: `app/api/routers/honeypot.py`
      Change: replace inline `x_monitor_key != settings.MONITOR_API_KEY` compare on `/honeypot/status` with `dependencies=[Depends(require_monitor_key)]`; response drops `allowlist_mode`, `allowlist_size`, keeps `mode_enabled`, `receiver_url_configured`, `secret_configured` (already bool), adds `allowlist_configured: bool`.
      Acceptance: existing tests pass; new `test_honeypot_status_requires_monitor_key` passes; wrong-key request returns 403 with same latency envelope as `/monitor/stats`.
      Rollback: `git revert <commit>`.

- [ ] **T07 — Idempotent broadcast: record + check `broadcast_message_id`**
      Finding: INTR-001
      Files: `app/services/broadcaster_srv.py`, `app/workers/tasks/flow_tasks.py`
      Change: `BroadcasterService.send_message(...)` returns the Telegram `message_id`. `_broadcast_logic` writes it to `broadcast_message_id` in the same UPDATE that flips `is_broadcasted=true`. On retry (`broadcast_message_id IS NOT NULL`), skip send and mark broadcasted.
      Acceptance: unit test in `tests/unit/test_broadcast_idempotency.py` simulating retry with pre-set `broadcast_message_id` does not call the send path.
      Rollback: `git revert <commit>`.

- [ ] **T08 — Claim-before-dispatch honeypot sweep + correct reason strings**
      Finding: INTR-002, CONC-002, LOGIC-005
      Files: `app/workers/tasks/flow_tasks.py`
      Change: `_honeypot_redirect_sweep_logic` claims `redirected_at='pending'` in an atomic UPDATE with `WHERE ... AND redirected_at IS NULL`; only processes claimed rows. `_honeypot_redirect_one_logic` returns `reason="mode_disabled"` (when `HONEYPOT_REDIRECT_MODE=False`) vs `"not_authorized"` (when `HONEYPOT_REDIRECT_AUTHORIZED=False`).
      Acceptance: unit test simulates two overlapping sweep calls against the same fixture and asserts each row is dispatched exactly once.
      Rollback: `git revert <commit>`.

- [~] **T09 — SKIPPED (operator accepts plaintext)**
      Finding: SEC-001 → Deferred
      Change: extension continues writing raw tokens directly to Supabase per operator preference. No changes to `extension/background.js` in this cycle.
      Acceptance: n/a.
      Rollback: n/a.

- [ ] **T10 — Write deploy runbook `docs/deployment/rebuild.md`**
      Finding: REL-001, SEC-003, FE-001, DRIFT-001, DRIFT-002
      Files: `docs/deployment/rebuild.md` (new)
      Change: full runbook — apply migrations in order (T02–T04, T15, T16, T17), run re-encrypt script, rebuild+roll containers, redeploy Vercel, verification checklist. NOT executed here — operator runs.
      Acceptance: file exists; every command is copy-pastable; expected outputs recorded.
      Rollback: `git rm docs/deployment/rebuild.md`.

- [ ] **T11 — Session A close: run test suite + commit boundary marker**
      Finding: protocol
      Files: `execute_state.json`, `bugfix.md`, `tasks.md`
      Change: run `ruff check app/`, `pytest -q`, `cd frontend && npm test`. Update `bugfix.md` statuses. Write STATE line. Commit boundary.
      Acceptance: all three commands pass or match baseline; state file updated.
      Rollback: none — status update.

**Session A gate**: I stop here and report. Operator approves before Session B.

---

## Session B — P1 Reliability & Interruption Safety

- [ ] **T12 — Write migration `20260906000004_media_hashes_failure_flag.sql`**
      Finding: INTR-005, CONC-003
      Files: `supabase/migrations/20260906000004_media_hashes_failure_flag.sql`
      Change: `ADD COLUMN IF NOT EXISTS is_failure BOOLEAN DEFAULT FALSE, ADD COLUMN IF NOT EXISTS failure_reason TEXT`; `UPDATE ... SET is_failure=TRUE WHERE sha256 LIKE '__failed__%'`; `CREATE UNIQUE INDEX IF NOT EXISTS idx_media_hashes_message_id_unique ON media_hashes(message_id)` (or a partial-unique if the sentinel scheme created duplicates).
      Acceptance: file exists; SQL parses.
      Rollback: `git rm`.

- [ ] **T13 — Write migration `20260906000005_audit_logs_composite_idx.sql`**
      Finding: PERF-001
      Files: `supabase/migrations/20260906000005_audit_logs_composite_idx.sql`
      Change: `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_event_type_timestamp ON audit_logs (event_type, timestamp DESC)`. Add rollback.
      Acceptance: file exists; SQL parses.
      Rollback: `git rm`.

- [ ] **T14 — Tighten audit scope + payload cap + regex**
      Finding: DATA-002, DATA-006, LOGIC-007
      Files: `app/core/audit.py`
      Change: (a) drop `SCRAPE_STRATEGY_ATTEMPT` from `_should_persist` list; (b) cap `details` serialised JSON to 8192 bytes in `_persist_to_db` — replace overflow with `{"__truncated": true, "kept_keys": [...], "original_size": N}`; (c) tighten `_TOKEN_RE` to `\b\d{8,15}:[A-Za-z0-9_-]{35}\b`.
      Acceptance: unit test on `_redact_details` with a fake `123:short_stuff` string is NOT redacted; a `1234567890:aa...(35 chars)` IS redacted. Unit test on `_persist_to_db` with 20 KB details caps to 8 KB. Insert count of `SCRAPE_STRATEGY_ATTEMPT` to `audit_logs` drops to zero after change.
      Rollback: `git revert`.

- [ ] **T15 — Media hash failure-flag column swap**
      Finding: INTR-005, LOGIC-002, DATA-004
      Files: `app/workers/tasks/flow_tasks.py:_hash_exfil_media_logic`
      Change: (a) skip download if `not file_meta.get('file_id')` (LOGIC-002/DATA-004); (b) on download failure, insert `{'is_failure': True, 'failure_reason': error}` instead of `sha256='__failed__<id>'`; (c) filter existing candidates using `.is_('is_failure', False)` (backfill from migration T12 makes this equivalent).
      Acceptance: unit test: dry-run against fixture with empty `file_meta` produces zero DB writes; explicit failure produces a row with `is_failure=True`.
      Rollback: `git revert`.

- [ ] **T16 — Broadcast permanent-failed cap**
      Finding: DATA-003, REL-003
      Files: `app/workers/tasks/flow_tasks.py`
      Change: read `MAX_BROADCAST_ATTEMPTS = int(os.getenv("MAX_BROADCAST_ATTEMPTS", 8))`. When `broadcast_attempts >= MAX_BROADCAST_ATTEMPTS`, set `is_broadcasted=true, broadcast_status='permanent_failed'` in a single UPDATE and skip further retry.
      Acceptance: unit test: row with `broadcast_attempts=8` and non-null `broadcast_error` is transitioned by the next broadcast pass; `is_broadcasted=true`, `broadcast_status='permanent_failed'`.
      Rollback: `git revert`.

- [ ] **T17 — CSV import breadcrumb**
      Finding: INTR-003
      Files: `app/workers/tasks/import_tasks.py`
      Change: after successful move to `imports/processed/`, touch `imports/processed/<name>.done`. On startup, skip `.pending → .csv` recovery if breadcrumb exists.
      Acceptance: manual kill test: kill mid-import, restart; the file with a `.done` breadcrumb is not recovered.
      Rollback: `git revert`.

- [ ] **T18 — Broadcast log line on lock skip + metric on disabled run**
      Finding: CONC-001, LOGIC-003
      Files: `app/workers/tasks/flow_tasks.py`, `app/core/metrics.py`
      Change: `logger.info("[Broadcast] Skipped — lock held")` on acquire-fail; `metrics.inc("broadcast.disabled_run")` on the disabled-early-return.
      Acceptance: `/health/metrics` shows counter after next beat fire with disabled flag.
      Rollback: `git revert`.

- [ ] **T19 — Add findings canary**
      Finding: LOGIC-001
      Files: `app/workers/tasks/flow_tasks.py`, `app/workers/celery_app.py` beat schedule
      Change: new `@app.task(name="flow.canary_findings_check")` task: insert a synthetic finding into `findings` table, verify it lands, `DELETE` synthetic row. Add beat entry every 60 min.
      Acceptance: task returns `{status: "ok"}` when `findings` table exists; returns `{status: "disabled", reason: "findings_table_missing"}` gracefully when not.
      Rollback: `git revert`.

- [ ] **T20 — Add flower + frontend healthchecks**
      Finding: REL-002
      Files: `docker-compose.yml`
      Change: `healthcheck: test: ["CMD", "wget", "--spider", "-q", "http://localhost:5555/"]` for flower; same pattern for frontend at port 3000. `alpine` base doesn't have wget on flower — use `curl -f` or `python -c "import urllib.request; urllib.request.urlopen(...)"`. Frontend uses next standalone which has neither — use `node -e "require('http').get('http://localhost:3000/', r => process.exit(r.statusCode < 500 ? 0 : 1))"`.
      Acceptance: `docker inspect ... --format '{{.State.Health.Status}}'` returns non-empty after next up.
      Rollback: `git revert`.

- [ ] **T21 — Session B close**
      Finding: protocol
      Same shape as T11.

**Session B gate**.

---

## Session C — P2 Hardening

- [ ] **T22 — TLS opt-in for webhook probes**
      Finding: SEC-004
      Files: `app/core/config.py`, `app/workers/tasks/flow_tasks.py`
      Change: add `TLS_VERIFY_WEBHOOK_PROBES: bool = False` to Settings; gate `verify=` in `flow_tasks.py:1643,1847` on that setting.
      Acceptance: default behavior unchanged; setting True enforces verify.
      Rollback: `git revert`.

- [ ] **T23 — Honeypot receive body-size cap**
      Finding: SEC-005
      Files: `app/api/routers/honeypot.py`
      Change: wrap request body read in `asyncio.wait_for(request.body(), timeout=5)`; assert `len(body) < 1_048_576`; else return HTTP 200 with `{"ok": true}` (Telegram must not retry) and log `[Honeypot] oversized payload dropped`.
      Acceptance: 2 MB POST is silently dropped with a log line; sane payloads succeed.
      Rollback: `git revert`.

- [ ] **T24 — Move `/monitor/stats` cache to Redis**
      Finding: PERF-004
      Files: `app/api/routers/monitor.py`
      Change: replace `_STATS_CACHE` per-process global with `redis_client.get('monitor:stats')` / `SETEX 30`.
      Acceptance: concurrent-worker cache miss issues one Supabase query; the other three workers read Redis.
      Rollback: `git revert`.

- [ ] **T25 — Scanner AsyncClient pooling audit**
      Finding: PERF-006
      Files: `app/services/scanners.py`, `app/services/scanners_extension.py`, `app/utils/http_client.py`
      Change: switch `httpx.AsyncClient(timeout=10.0)` allocations in scanner services to `get_async_http_client(timeout=10.0)` where available. Do NOT touch the `verify=False` sites (SEC-004 handles them separately).
      Acceptance: `git grep -n 'httpx.AsyncClient(' -- 'app/services/scanners*.py'` count drops by ≥ 60 %.
      Rollback: `git revert`.

- [ ] **T26 — Extension regex alignment**
      Finding: DATA-005
      Files: `extension/content.js`, `extension/background.js`, `app/utils/helpers.py`
      Change: grep extension for token regex; align to `\d{8,15}:[A-Za-z0-9_-]{35}` if drifted. If identical to code: mark DATA-005 **Invalid** in bugfix.md.
      Acceptance: grep of all four sites returns the same regex string.
      Rollback: `git revert`.

- [ ] **T27 — Delete `SERPER_API_KEY`, `CENSYS_ID`, `CENSYS_SECRET`, `HYBRID_ANALYSIS_KEY`**
      Finding: DEAD-001, DEAD-002, DRIFT-005
      Files: `app/core/config.py`, `.env.template`
      Change: remove the four Settings fields; remove `SERPER_API_KEY=` from `.env.template`.
      Acceptance: `ruff check` clean; grep confirms no references.
      Rollback: `git revert`.

- [ ] **T28 — Rename `frontend/vite.config.ts` → `frontend/vitest.config.ts`**
      Finding: REL-005, STRUCT-004
      Files: `frontend/vite.config.ts` → `frontend/vitest.config.ts`
      Change: `git mv`; verify `frontend/package.json` scripts still work (`npm test` uses `vitest` auto-discovery).
      Acceptance: `cd frontend && npm test` passes.
      Rollback: `git mv` reverse.

- [ ] **T29 — README fresh-deploy prerequisite + gitignore cleanup**
      Finding: REL-004, STRUCT-002
      Files: `README.md`, `.gitignore`
      Change: (a) add a "Fresh deployment prerequisite" section to README documenting `docker volume create telegramhunter_{redis_data,sessions,imports,beat_schedule}` (aliased as `${*_VOLUME_NAME}`); (b) remove the `!.env.example` line from `.gitignore` (whitelist entry with no matching file).
      Acceptance: README diff is additive; gitignore diff is a single-line removal.
      Rollback: `git revert`.

- [ ] **T30 — Session C close**
      Finding: protocol
      Same shape as T11.

**Session C gate**.

---

## Session D — Structural Moves & Historical Archive

Every move uses `git mv`. Protected files (migrations in `database/migrations/`) are relocated only — never deleted — per safety rule.

- [ ] **T31 — Preserve pre-cycle root doc snapshot** (already done in Phase 0 for `bugfix.md`/`design.md`/`tasks.md`/AUDIT.md, expand for the rest)
      Finding: STRUCT-001
      Files: create `docs/history/pre_02execute_20260906/{AUDIT_LOG.md,security_audit.md,PRD.md,SUPABASE_KEEPALIVE_SETUP.md,plan.html,competitive-upgrade-plan.html,competitive-upgrade-plan-CORRECTED.html}`
      Change: `cp` each of the 7 root files into the preserve directory as an insurance backup before `git mv`.
      Acceptance: 7 files present in preserve dir.
      Rollback: `rm docs/history/pre_02execute_20260906/*`.

- [ ] **T32 — Move `AUDIT_LOG.md` → `docs/history/AUDIT_LOG.md`**
      Finding: STRUCT-001, DEAD-003
      Files: `AUDIT_LOG.md` → `docs/history/AUDIT_LOG.md`
      Change: `git mv`.
      Acceptance: file moved; no code references it.
      Rollback: `git mv` reverse.

- [ ] **T33 — Move `security_audit.md` → `docs/history/security_audit.md`**
      Finding: STRUCT-001, DEAD-003
      Files: `security_audit.md` → `docs/history/security_audit.md`
      Change: `git mv`.
      Acceptance: moved.
      Rollback: reverse.

- [ ] **T34 — Move `PRD.md` → `docs/PRD.md`**
      Finding: STRUCT-001
      Files: `PRD.md` → `docs/PRD.md`
      Change: `git mv`. Content rewrite is 03_DOCUMENT's job; here we just relocate.
      Acceptance: moved.
      Rollback: reverse.

- [ ] **T35 — Rewrite `SUPABASE_KEEPALIVE_SETUP.md` → `docs/SUPABASE_KEEPALIVE.md`**
      Finding: DATA-007, DRIFT-007
      Files: `SUPABASE_KEEPALIVE_SETUP.md` → `docs/SUPABASE_KEEPALIVE.md`
      Change: `git mv`, then rewrite content: `keepalive_log` (singular) everywhere; drop plural `keepalive_logs` references; align cron cadence to `.github/workflows/supabase-keep-alive.yml` (daily 08:00 UTC).
      Acceptance: file moved and rewritten; grep `keepalive_logs` returns zero hits.
      Rollback: reverse move + revert rewrite.

- [ ] **T36 — Move 3 root HTML artifacts → `docs/history/`**
      Finding: STRUCT-001, FS-003
      Files: `plan.html`, `competitive-upgrade-plan.html`, `competitive-upgrade-plan-CORRECTED.html` → `docs/history/`
      Change: `git mv` × 3.
      Acceptance: moved.
      Rollback: reverse.

- [ ] **T37 — Move `database/migrations/*.sql` → `docs/history/legacy_migrations/`** (**PROTECTED FILES — mv only, never delete**)
      Finding: STRUCT-007
      Files: `database/migrations/*.sql` (8 files) + `database/migrations/README.md` → `docs/history/legacy_migrations/`
      Change: `git mv` per file, ensuring each transfer preserves history. Verify with `git log --follow` post-move.
      Acceptance: 8 files + README present in new dir; `git log --follow` on each shows continuity; `database/migrations/` dir empty (kept for future — do not `rmdir`).
      Rollback: reverse `git mv` per file.

- [ ] **T38 — `.gitignore` and untrack local tooling state**
      Finding: FS-001, FS-002, STRUCT-002, DEAD-006
      Files: `.gitignore`, `.claude/*.lock`, `.claude/settings.local.json`, `.playwright-mcp/*.yml`
      Change: (a) append `.claude/*.lock`, `.claude/settings.local.json`, `.playwright-mcp/` to `.gitignore`; (b) `git rm --cached .claude/scheduled_tasks.lock .claude/settings.local.json .playwright-mcp/page-2026-04-24T02-45-32-072Z.yml`.
      Acceptance: `git status` shows the three files as untracked but present locally; `.gitignore` diff is additive.
      Rollback: `git checkout HEAD -- .gitignore`, `git add -f` the removed files.

- [ ] **T39 — Session D close**
      Finding: protocol
      Same shape as T11.

**Session D gate**.

---

## Session E — P3 Cleanup

- [ ] **T40 — `hmac.compare_digest` on bot admin path**
      Finding: SEC-006
      Files: `app/services/bot_listener.py`
      Change: replace `==` on numeric bot-id string comparison in the admin gate path with `hmac.compare_digest(str(a), str(b))`.
      Acceptance: unit test on the admin gate passes; existing tests pass.
      Rollback: `git revert`.

- [ ] **T41 — Reorder own-bot check in `ingest`**
      Finding: LOGIC-006
      Files: `app/api/routers/ingest.py`
      Change: move `_is_own_bot_token(token)` call to before `security.encrypt(token)` in the "new record" branch.
      Acceptance: rejection path never enters `security.encrypt`; ruff + tests clean.
      Rollback: `git revert`.

- [ ] **T42 — Log beat schedule digest at worker_ready**
      Finding: REL-006
      Files: `app/workers/celery_app.py`
      Change: in `on_worker_ready`, log `INFO [Beat] schedule digest sha256=... N tasks` derived from sorted task names.
      Acceptance: log line appears at worker start.
      Rollback: `git revert`.

- [ ] **T43 — Move `retry_with_backoff` → `app/utils/http_client.py`**
      Finding: DEAD-007
      Files: `app/services/scanners.py` (remove), `app/utils/http_client.py` (add), scanner files (update imports)
      Change: extract helper, add import at top of scanner files.
      Acceptance: `git grep -n 'retry_with_backoff' -- 'app/**/*.py'` shows the helper's new home + callers only.
      Rollback: `git revert`.

- [ ] **T44 — Add CI anon-key smoke test**
      Finding: FE-002
      Files: `.github/workflows/ci.yml`
      Change: add a job step that curls `discovered_credentials_public` with anon key and expects 401 or empty body.
      Acceptance: CI YAML valid; workflow step runs in a follow-up PR.
      Rollback: `git revert`.

- [ ] **T45 — Author broad-except inventory**
      Finding: DEAD-008 (deferred fix, inventory only)
      Files: `docs/history/broad_except_review.md` (new)
      Change: run `git grep -nE 'except Exception:' -- 'app/**/*.py'` and dump into the file with categorised buckets (best-effort vs. real error swallowing). Do not fix any of them here.
      Acceptance: file lists all 142 sites with file:line and one-line categorisation.
      Rollback: `git rm`.

- [ ] **T46 — DEAD-005 activity check + removal**
      Finding: DEAD-005
      Files: `.deepsource.toml`, `.sourcery.yml` (potentially removed)
      Change: check for DeepSource/Sourcery activity via `git log`, `gh api checks`, README badges. If both inactive: `git rm .deepsource.toml .sourcery.yml`. If either active: leave in place with an explanatory comment.
      Acceptance: either both files removed or a short note in bugfix.md explaining why they stay.
      Rollback: `git checkout HEAD -- .deepsource.toml .sourcery.yml`.

- [ ] **T47 — DEAD-008 top-20 broad-except fix**
      Finding: DEAD-008 (partial)
      Files: various `app/**/*.py` — top ~20 worst offenders identified by T45 inventory
      Change: replace `except Exception: pass` with `except Exception as e: logger.debug(f"[<module>] suppressed: {e}")` in bare-swallow sites (best-effort telemetry paths where a broad catch is acceptable). Do NOT change error-handling semantics anywhere the exception should actually propagate.
      Acceptance: 20+ sites patched; `pytest -q` + `ruff check app/` clean.
      Rollback: `git revert`.

- [ ] **T48 — `docker compose build --no-cache` + `up -d`**
      Finding: REL-001 (operator delegated)
      Files: none — runtime action.
      Change: (a) commit all Session A-E code changes first; (b) `docker compose build --no-cache api bot worker-core worker-scanners worker-scrape worker-validators beat flower frontend`; (c) `docker compose up -d`; (d) wait 60 s for stack to settle; (e) verify each container is `healthy` (or up for those without healthchecks); (f) hit `/health/` and `/health/queues` with monitor key and record output. If any container is unhealthy after 3 min, stop and report.
      Acceptance: `docker inspect` shows fresh `Created` timestamps on all 9 services; `/health/` returns 200; queue depths visible.
      Rollback: `docker compose down` (no `-v`), pull the pre-cycle image tags (recorded in T01 baseline), `docker compose up -d`.

- [ ] **T49 — Session E close + final closeout**
      Finding: protocol + Phase 4
      Change: update `bugfix.md` (mark all Fixed / Deferred / Invalid); update `tasks.md` (all `[x]`); write `execute_state.json` with final counts; delete only `execute_state.json` at cleanup, keep the other artifacts as the trail.
      Acceptance: closeout report emitted with Phase 4 shape.
      Rollback: none — protocol.

---

## Task ordering rationale

- **T01** captures baseline before anything else so regressions are visible.
- **T02–T04** are migration files (inert until applied) — safe to ship first.
- **T05** is the re-encrypt script (inert until run).
- **T06–T09** are code changes that don't depend on migrations being applied yet — pure code hardening in the current image.
- **T10** is the runbook that ties T02–T09 together for the operator.
- **T11** closes Session A cleanly.
- **T12–T20** are P1 reliability fixes; some depend on Session A code being present (T15 uses new column layout implied by T12, T16 uses T03).
- **T21** closes Session B.
- **T22–T30** are P2 hardening: TLS opt-in, body-size cap, cache move, dead env removal, filename rename, README.
- **T31–T39** are structural moves. Ordered LAST among behavioral work per prompt so path references in earlier tasks are still valid when those tasks run.
- **T40–T46** are P3 hygiene and the inventory file for DEAD-008.

Dead-code deletion (DRIFT-005 removed env vars in T27, DEAD-006 gitignore in T38) is done **after** their replacements (or lack thereof) are verified. No file with historical evidence is deleted — only moved.

---

## Approval requested

Phase 1 artifacts are complete: `bugfix.md` (69 findings ledger), `design.md` (implementation plan with migrations, blast radius, rollback), `tasks.md` (46 sequenced atomic tasks across 5 sessions).

**Please review before I begin Phase 3.** In particular, confirm:

1. **Migration application by operator, not me** — the 5 new migration files (T02, T03, T04, T12, T13) plus the re-encrypt script (T05) will be staged by me. **I will not `psql` or `supabase db push` against the live DB.** You apply. If you'd rather I execute them (with your explicit go-ahead per migration), say so and I'll add a T-step accordingly — but the default is you-run.

2. **Docker rebuild + Vercel deploy** — the runbook (T10) is a document. I do not `docker compose build` or trigger Vercel; you do. Confirm this split.

3. **Deferrals** — PERF-002, PERF-003, DEAD-005, DEAD-008 are marked Deferred with rationale. If you want any of them in scope, tell me.

4. **Session boundaries** — I stop after A (T11), B (T21), C (T30), D (T39), E (T46) for review. If you'd rather I run A→E without stopping unless something breaks, say `run all sessions`.

5. **`git mv` chain in Session D** — moves happen on this branch. Pre-cycle backups exist at `docs/history/pre_02execute_20260906/`. Confirm you're comfortable with the file layout after T31–T39.

Answer with `approved` (proceed A→E with stops), `approved — run all sessions` (proceed continuously), or specific amendments.
