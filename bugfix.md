# bugfix.md — Defect Ledger for 02_EXECUTE Cycle

Source: `AUDIT.md` (commit `3a033cfe06123627c37cbfb03662a9a543c61ebf`).
Selection: `fix all` — 69 findings.
Branch: `remediation/2026-09-06`.
Historical `bugfix.md` (pre-cycle) preserved at `docs/history/pre_02execute_20260906/bugfix.md`.

Status values: `Open`, `In Progress`, `Fixed`, `Blocked`, `Invalid`, `Deferred`.

---

## SEC-001 — Encrypt 478 plaintext bot tokens; close extension raw-write path
- Status: **Deferred** — operator explicitly accepts plaintext-at-rest for revoked tokens (2026-09-06). Fernet pipeline stays intact for new inserts via `/ingest` and the server-side self-heal on active tokens. No re-encryption of the 478 historical plaintext rows. No RLS policy removal. No CHECK constraint. Extension direct-write path (T09) also deferred — extension continues writing raw, matching operator preference.
- Severity: P0 (finding severity retained; risk explicitly accepted)
- Root cause: RLS policy on `discovered_credentials` allows anon `INSERT`/`UPDATE` with `x-extension-secret` header, and the extension writes tokens raw. `flow.exfiltrate_chat`'s self-heal only fires during exfiltration, so tokens that never go active remain plaintext at rest.
- Impact: 478 / 1000 sampled rows are plaintext (47.8 %). Any RLS bypass or service-role key leak exposes live bot tokens. Operator accepts this risk.
- Files: not modified in this cycle.
- Fix approach: n/a (deferred).
- Verification: n/a.
- Blocked by: none — explicit operator decision.

## SEC-002 — Non-constant-time compare on `/honeypot/status`
- Status: Open
- Severity: P1
- Root cause: `app/api/routers/honeypot.py:151` uses inline `!=` on `X-Monitor-Key` header instead of `require_monitor_key` dependency.
- Impact: Timing side-channel on operator key.
- Files: `app/api/routers/honeypot.py`.
- Fix approach: Replace inline check with `Depends(require_monitor_key)` on the route decorator; drop the manual header extraction.
- Verification: `curl -H "X-Monitor-Key: wrong" .../honeypot/status` returns 403 with the same latency envelope as `/monitor/stats`; existing tests still pass.
- Blocked by: none.

## SEC-003 — Frontend release drift exposes findings without auth
- Status: Open
- Severity: P1
- Root cause: Vercel + local Docker frontend running SHA `84e2e47` per `tasks.md:P0-005`; new RLS on `discovered_credentials_public` grants only `authenticated`.
- Impact: Anonymous access to findings dashboard until redeploy.
- Files: none (deployment op).
- Fix approach: Consolidated with REL-001 rebuild + Vercel redeploy (see below).
- Verification: `curl -H "apikey: $SUPABASE_ANON_KEY" $SUPABASE_URL/rest/v1/discovered_credentials_public?limit=1` returns 401 (or empty payload with permission-denied for authenticated-only view).
- Blocked by: REL-001.

## SEC-004 — TLS verification disabled in scanners and webhook probes
- Status: Open
- Severity: P2
- Root cause: 13 sites use `httpx.AsyncClient(verify=False)`. Scanner probes are intentional (self-signed OSINT targets); webhook probes should honour TLS validity as an intel signal.
- Impact: Cert-mismatch on captured C2 hosts is not observable; scanner probes could be MITM'd.
- Files: `app/services/scanners.py`, `app/services/scanners_extension.py`, `app/workers/tasks/flow_tasks.py`.
- Fix approach: Add `TLS_VERIFY_WEBHOOK_PROBES` bool setting (default False for backward-compat), gate `flow_tasks.py:1643,1847` on it. Leave scanner-probe sites as-is (intentional) but add an inline comment referencing the design decision.
- Verification: `grep -c verify=False` unchanged in scanners; `flow_tasks.py` webhook probe sites read from settings.
- Blocked by: none.

## SEC-005 — Missing body-size cap on `/honeypot/receive/{id}`
- Status: Open
- Severity: P2
- Root cause: `await request.json()` reads unbounded body; FastAPI default is large.
- Impact: Oversized payloads waste memory and could hit Supabase JSONB size limits.
- Files: `app/api/routers/honeypot.py`.
- Fix approach: Read `request.body()` with `asyncio.wait_for(..., timeout=5)`, assert `len(body) < 1_048_576`, then parse.
- Verification: `curl -X POST -d @1MB.bin .../honeypot/receive/uuid` returns 413.
- Blocked by: none.

## SEC-006 — Non-constant-time bot-id compare in listener
- Status: **Invalid** — verification 2026-09-06
- Severity: P3
- Root cause verification: I re-read `app/services/bot_listener.py`. The admin-check function `is_admin` compares `user.id == ANONYMOUS_ADMIN_ID` — two integers, not string secrets, and `ANONYMOUS_ADMIN_ID` is the fixed public Telegram anonymous-admin bot ID `1087968824`. It is not a secret; timing side-channel does not apply. `_bot_id_from_token` is used only for lock keys and log strings, never for authorisation. Audit description conflated an internal identifier compare with an auth gate. No fix needed.
- Files: verified `app/services/bot_listener.py:143-186`.
- Fix approach: n/a — no compare hardening required.
- Verification: comparison is on Python ints and the second operand is not confidential; `hmac.compare_digest` requires bytes/str and provides no benefit here.
- Blocked by: none.

---

## DATA-001 — Four migrations not applied on live Supabase
- Status: Open
- Severity: P0
- Root cause: `supabase/migrations/20260806000001_honeypot_redirect.sql`, `20260904000002_insight_queue.sql`, `20260904000003_entities_engagement.sql`, `20260904000004_finding_alert_policies.sql`, `20260904000005_monitor_findings_feedback.sql`, `20260906000001_dashboard_operator_authorization.sql` — verify each is applied.
- Impact: `/monitor/findings/*` returns 500; `flow.produce_findings`, `flow.build_entity_graph`, honeypot multi-touch tasks all silent-fail.
- Files: `supabase/migrations/` (already exist); operator action to apply.
- Fix approach: Generate a consolidated `docs/migrations/pending_apply.md` listing exact migration IDs, DDL locations, and `supabase db push` / SQL-editor instructions. I do NOT run DDL against live DB (safety rule).
- Verification: `SELECT count(*) FROM findings, finding_evidence, engagement_events, honeypot_redirect_log` returns valid counts (any number, including 0); `/monitor/findings` returns `[]` instead of 500.
- Blocked by: none (user applies).

## DATA-002 — audit_logs table bloat (357k rows)
- Status: Open
- Severity: P1
- Root cause: `SCRAPE_STRATEGY_ATTEMPT` and `BROADCAST_FAILED` dominate insertions; weekly prune retains 90 days.
- Impact: Query performance on `/health/operational` degrades; storage grows.
- Files: `app/core/audit.py`.
- Fix approach: (1) Drop `SCRAPE_STRATEGY_ATTEMPT` from `_should_persist`; (2) Cap `details` payload size to 8 KB in `_persist_to_db`; (3) See PERF-001 for composite index.
- Verification: New audit inserts have ≤ 8 KB `details`; scrape strategy attempts still logged to stdout, no longer persisted.
- Blocked by: none.

## DATA-003 — 1478 broadcasts stuck in retry loop
- Status: Open
- Severity: P1
- Root cause: Failed broadcasts (mostly `media_archive_not_found`) increment `broadcast_attempts` without a cap; `next_retry_at` keeps rescheduling indefinitely.
- Impact: Retry queue grows; broadcast worker wastes cycles.
- Files: `app/workers/tasks/flow_tasks.py`, `app/services/broadcaster_srv.py`; new migration for a `broadcast_status` enum column.
- Fix approach: Add hard cap `MAX_BROADCAST_ATTEMPTS=8`; when reached, set `is_broadcasted=true` AND record `broadcast_status='permanent_failed'` (new column) instead of infinite retry. Do not delete rows.
- Verification: `SELECT count(*) FROM exfiltrated_messages WHERE broadcast_attempts >= 8 AND is_broadcasted=false` = 0 after next broadcast run.
- Blocked by: none.

## DATA-004 — `hash_exfil_media` fetches rows with empty `file_meta`
- Status: Open
- Severity: P2
- Root cause: Filter `not_.is_("file_meta", "null")` accepts `{}` JSONB values. Rows without `file_id` still trip the download path.
- Impact: `media_hashes` grows sentinel rows (`sha256=__failed__<id>`) for legitimate no-media rows.
- Files: `app/workers/tasks/flow_tasks.py:hash_exfil_media`.
- Fix approach: Python-side skip if `file_meta.get('file_id')` is falsy before download.
- Verification: `hash_exfil_media` run produces 0 new sentinel rows over 30 min.
- Blocked by: none.

## DATA-005 — Token regex inconsistency (unverified in extension)
- Status: Open
- Severity: P2
- Root cause: `bugfix.md` BUG-012 flagged three sites; extension side not re-checked in the audit.
- Impact: Valid tokens rejected or invalid ones accepted by extension.
- Files: `extension/content.js`, `extension/background.js`, `app/utils/helpers.py`, `app/services/scanners.py`.
- Fix approach: Grep extension for the regex; align to `\d{8,15}:[A-Za-z0-9_-]{35}` exactly. If it already matches, mark **Invalid**.
- Verification: All four regex sites identical; extension smoke-test rejects a 34-char secret.
- Blocked by: none.

## DATA-006 — `audit_logs.details` payload has no size cap
- Status: Open
- Severity: P2
- Root cause: `AuditLogger._persist_to_db` writes any `details` dict; only exceptions are pre-truncated to 500 chars.
- Impact: Large scrape evidence blobs bloat rows.
- Files: `app/core/audit.py`.
- Fix approach: Cap serialised JSON to 8 KB in `_persist_to_db`; replace with `{"...":"truncated", "kept_keys":[...]}` if over cap.
- Verification: Insert of large details dict lands ≤ 8 KB.
- Blocked by: none.

## DATA-007 — `keepalive_log` vs `keepalive_logs` name drift
- Status: Open
- Severity: P3
- Root cause: `SUPABASE_KEEPALIVE_SETUP.md` describes plural table; canonical schema is singular.
- Impact: Confusion; no runtime failure — GitHub workflow uses correct singular name.
- Files: `SUPABASE_KEEPALIVE_SETUP.md` (rewrite + move) — see STRUCT-001.
- Fix approach: Rewrite doc against `keepalive_log`.
- Verification: Doc references `keepalive_log` (singular) exactly.
- Blocked by: none.

---

## CONC-001 — Broadcast lock skip is not logged
- Status: Open
- Severity: P1
- Root cause: When another worker holds the broadcast lock, the task returns a string with no log call.
- Impact: Operators can't tell if beat is firing at all.
- Files: `app/workers/tasks/flow_tasks.py:broadcast_pending`.
- Fix approach: `logger.info("[Broadcast] Skipped — lock held by another worker")` on the acquire-fail path.
- Verification: Log line appears when beat fires while another run is active.
- Blocked by: none.

## CONC-002 — Honeypot redirect sweep double-dispatch window
- Status: Open
- Severity: P2
- Root cause: Two overlapping sweeps can both dispatch `honeypot_redirect_one` for the same `honeypot_updates.id` before `redirected_at` is set post-send.
- Impact: Duplicate DM to victim.
- Files: `app/workers/tasks/flow_tasks.py:_honeypot_redirect_sweep_logic`.
- Fix approach: Claim-before-dispatch — `UPDATE honeypot_updates SET redirected_at='pending' WHERE id=? AND redirected_at IS NULL RETURNING id`; process only claimed rows.
- Verification: Two sweep runs against the same fixture return zero duplicate dispatches.
- Blocked by: INTR-002 (same code area — combine into one commit).

## CONC-003 — `media_hashes` UNIQUE(message_id) unverified; race window on concurrent hash runs
- Status: Open
- Severity: P2
- Root cause: Two `hash_exfil_media` runs can both compute the same candidate set. If no `UNIQUE(message_id)` exists, duplicate rows appear.
- Impact: `media_hashes` bloat, incorrect duplicate reports.
- Files: `supabase/migrations/20260906000003_media_hashes_unique.sql` (new).
- Fix approach: Verify existing migration; if missing, ship a migration adding `UNIQUE(message_id)` and switch inserts to `ON CONFLICT (message_id) DO NOTHING`.
- Verification: `SELECT count(*) FROM (SELECT message_id, count(*) AS c FROM media_hashes GROUP BY message_id HAVING count(*) > 1) t` returns 0.
- Blocked by: none.

## CONC-004 — Two lock primitives coexist
- Status: Open
- Severity: P2
- Root cause: `RedisService.acquire_lock` uses `SET NX EX` with Lua CAS release; `broadcast_pending` uses `redis.lock(...)` native. Two APIs.
- Impact: Maintenance risk; not a runtime bug.
- Files: `app/core/redis_srv.py`, `app/workers/tasks/flow_tasks.py:broadcast_pending`.
- Fix approach: Add a docstring header on `RedisService` clarifying which primitive to use where. Do not change semantics — behavior is correct.
- Verification: Docstring present; no code change.
- Blocked by: none.

---

## INTR-001 — Broadcast send → DB update is non-atomic (duplicate broadcast on kill)
- Status: Open
- Severity: P1
- Root cause: Between successful Telegram `sendMessage` and DB `UPDATE ... SET is_broadcasted=true`, a worker kill re-triggers send after 15-min claim reclaim.
- Impact: Duplicate broadcast to monitor group.
- Files: new migration `supabase/migrations/20260906000004_broadcast_message_id.sql`; `app/services/broadcaster_srv.py`; `app/workers/tasks/flow_tasks.py:_broadcast_logic`.
- Fix approach: Add `broadcast_message_id BIGINT` column. On send success, record Telegram's returned message-id; on retry, if column is non-null, mark `is_broadcasted=true` without re-sending.
- Verification: Kill-worker test (manual): pause mid-broadcast between send and update; expect no duplicate on reclaim.
- Blocked by: none.

## INTR-002 — Honeypot redirect send → mark non-atomic
- Status: Open
- Severity: P2
- Root cause: `honeypot_redirect_one` sends via Telegram, then sets `redirected_at` + Redis dedup. Kill between = duplicate DM.
- Impact: Victim gets same redirect twice.
- Files: `app/workers/tasks/flow_tasks.py:_honeypot_redirect_one_logic`.
- Fix approach: Pair with CONC-002 — set `redirected_at='pending'` on claim (in sweep) before dispatch; on send-success flip to `now()`, on failure clear.
- Verification: Same as CONC-002.
- Blocked by: none.

## INTR-003 — CSV import recovers `.pending` blindly
- Status: Open
- Severity: P2
- Root cause: Startup renames `.pending → .csv` unconditionally; if the previous run half-processed the file, tokens re-enqueue.
- Impact: Wasted Telegram getMe budget (Redis dedup catches actual duplicates).
- Files: `app/workers/tasks/import_tasks.py`.
- Fix approach: Persist per-file breadcrumb `imports/processed/<name>.done`; skip `.pending` recovery if breadcrumb exists.
- Verification: Kill mid-import, restart, verify same file not re-enqueued.
- Blocked by: none.

## INTR-004 — Self-heal encrypt-before-persist window
- Status: Open
- Severity: P2
- Root cause: `_exfiltrate_logic` encrypts, DB-writes, then uses; on DB-write failure, raw token stays in memory for downstream.
- Impact: On rare failure path, raw token used downstream without persisting encrypted copy.
- Files: `app/workers/tasks/flow_tasks.py:_exfiltrate_logic`.
- Fix approach: If persist fails, raise and let Celery retry; do not proceed with raw token.
- Verification: Unit test simulating persist failure raises + no downstream call.
- Blocked by: SEC-001 partially subsumes this.

## INTR-005 — Media hash failure sentinel is a fragile string
- Status: Open
- Severity: P3
- Root cause: `sha256='__failed__<id>'` sentinel is a string convention; no boolean flag.
- Impact: Fragile against future `message_id` shape changes.
- Files: `supabase/migrations/20260906000005_media_hashes_failure_flag.sql`; `app/workers/tasks/flow_tasks.py:hash_exfil_media`.
- Fix approach: Add columns `is_failure BOOLEAN DEFAULT FALSE`, `failure_reason TEXT`; migrate existing sentinel rows (`UPDATE ... WHERE sha256 LIKE '__failed__%'`).
- Verification: New failed rows land with `is_failure=true`; sentinel column stays but is deprecated in code.
- Blocked by: none.

---

## LOGIC-001 — Canary disabled when raw broadcast disabled (no findings-canary)
- Status: Open
- Severity: P1
- Root cause: `canary_flow_check` bails if `ENABLE_RAW_MESSAGE_BROADCAST=False` (HEAD default). No alternative end-to-end canary exists for the findings pipeline.
- Impact: Post-drift-fix, there's no health signal.
- Files: new task `app/workers/tasks/flow_tasks.py:canary_findings_check` (or new module).
- Fix approach: Add a `flow.canary_findings_check` task that inserts a synthetic finding, verifies routing, cleans up. Schedule alongside existing canary.
- Verification: Task returns `{status: "ok"}` on green path.
- Blocked by: DATA-001 (needs `findings` table).

## LOGIC-002 — hash_exfil_media empty-file_meta filter
- Status: Open
- Severity: P1
- Root cause: Same as DATA-004 (LOGIC angle: the download attempt on empty file_meta is a wasted call).
- Impact: Wasted Telegram MTProto calls + `media_hashes` bloat.
- Files: `app/workers/tasks/flow_tasks.py:hash_exfil_media`.
- Fix approach: Same fix as DATA-004 — merge into one code change.
- Verification: Same.
- Blocked by: DATA-004 (same fix).

## LOGIC-003 — Broadcast disabled runs are silent
- Status: Open
- Severity: P2
- Root cause: `broadcast_pending` early-returns string with no metric.
- Impact: Operator can't see disabled-fire counter.
- Files: `app/workers/tasks/flow_tasks.py:broadcast_pending`, `app/core/metrics.py`.
- Fix approach: `metrics.inc("broadcast.disabled_run")` on the early return.
- Verification: `/health/metrics` shows counter incrementing.
- Blocked by: none.

## LOGIC-004 — `/honeypot/status` leaks operational posture
- Status: Open
- Severity: P2
- Root cause: Endpoint returns `mode_enabled`, `secret_configured`, `allowlist_size` verbatim.
- Impact: If monitor key leaks, endpoint fingerprints the deployment.
- Files: `app/api/routers/honeypot.py`.
- Fix approach: Reduce output to `{"mode_enabled": bool, "receiver_ready": bool}`. Drop `allowlist_size`, `allowlist_mode`, `secret_configured`.
- Verification: Response schema shrunk; existing monitor tests still pass.
- Blocked by: SEC-002 (same file — merge commit).

## LOGIC-005 — honeypot_redirect_one wrong reason string
- Status: Open
- Severity: P2
- Root cause: `MODE=False` returns `reason="not_authorized"` — misleading label.
- Impact: Triage confusion.
- Files: `app/workers/tasks/flow_tasks.py:_honeypot_redirect_one_logic`.
- Fix approach: Differentiate: `MODE=False` → `"mode_disabled"`; `AUTHORIZED=False` → `"not_authorized"`.
- Verification: Unit test both gates individually.
- Blocked by: none.

## LOGIC-006 — Own-bot check runs after encrypt in ingest
- Status: Open
- Severity: P3
- Root cause: `_is_own_bot_token` called after `security.encrypt(token)` and dict prep.
- Impact: Wasted CPU on rejection path.
- Files: `app/api/routers/ingest.py`.
- Fix approach: Move check before `security.encrypt`, right after `token_hash` calc.
- Verification: Existing tests pass; a rejection path no longer touches encrypt.
- Blocked by: none.

## LOGIC-007 — Overly permissive audit token regex
- Status: Open
- Severity: P3
- Root cause: `_TOKEN_RE = r'\b\d{5,15}[:%][A-Za-z0-9_-]{20,}\b'` — matches non-token strings.
- Impact: Over-redaction obscures unrelated content.
- Files: `app/core/audit.py`.
- Fix approach: Tighten to `\b\d{8,15}:[A-Za-z0-9_-]{35}\b`.
- Verification: New audit test with fake-looking numeric strings not matching real-token shape are not redacted.
- Blocked by: none.

---

## PERF-001 — `audit_logs` composite index missing
- Status: Open
- Severity: P2
- Root cause: `/health/operational` filters on `event_type + timestamp`; two single-column indexes force bitmap-heap-scan.
- Impact: Query latency at scale (357k rows already).
- Files: new migration `supabase/migrations/20260906000006_audit_logs_composite_idx.sql`.
- Fix approach: `CREATE INDEX CONCURRENTLY idx_audit_event_type_timestamp ON audit_logs(event_type, timestamp DESC)`.
- Verification: `EXPLAIN ANALYZE` on the operational query shows index scan.
- Blocked by: none.

## PERF-002 — Broadcast serial send
- Status: Deferred (P2, effort L — needs benchmark data before parallelising, per prompt intent constraints)
- Severity: P2
- Root cause: `_broadcast_logic` iterates messages with 1.5 s sleep between sends.
- Impact: Throughput capped at ≈ 40 msgs/min per beat run.
- Files: `app/workers/tasks/flow_tasks.py`.
- Fix approach: Would need per-topic `asyncio.gather` group. Preserved as design work — see `design.md` §Deferred.
- Verification: Requires latency benchmark before/after; out of scope for this cycle.
- Blocked by: none — deferred by scope discipline.

## PERF-003 — MTProto connect/disconnect per media download
- Status: Deferred (P2, effort M — coupled with PERF-002 timing benchmarks)
- Severity: P2
- Root cause: No pooling on media-download side.
- Impact: Log shows connect-per-message overhead.
- Files: `app/services/broadcaster_srv.py`.
- Fix approach: Reuse `TelegramClient` per worker; disconnect only on shutdown.
- Verification: Requires load test.
- Blocked by: PERF-002 timing decisions.

## PERF-004 — `/monitor/stats` cache is per-worker
- Status: Open
- Severity: P2
- Root cause: 4 uvicorn workers each hold their own 30 s cache.
- Impact: 4× Supabase load on cache miss.
- Files: `app/api/routers/monitor.py`.
- Fix approach: Move cache to Redis (`GETEX`/`SETEX monitor:stats`).
- Verification: Test that concurrent worker cache misses hit Supabase at most once.
- Blocked by: none.

## PERF-005 — refresh_pending_tokens sleep pacing
- Status: Invalid
- Severity: P3
- Root cause: Concern that inline sleeps could contend on the worker.
- Impact: None — validation is already on the dedicated `validation` queue via `apply_async(queue='validation')`.
- Files: verified `app/workers/tasks/validation_tasks.py:_refresh_pending_tokens_async`.
- Fix approach: n/a — verified operating as intended.
- Verification: Confirmed by code inspection.
- Blocked by: none.

## PERF-006 — Scanner AsyncClient per-call allocation
- Status: Open
- Severity: P3
- Root cause: Every scanner instantiates `httpx.AsyncClient(timeout=10.0)` per call.
- Impact: TLS handshake overhead per scanner run.
- Files: `app/services/scanners.py`, `app/services/scanners_extension.py`.
- Fix approach: Route through `app.utils.http_client.get_async_http_client` where present; add helper for scanner use if not.
- Verification: Grep for `httpx.AsyncClient` inside scanner classes shows uses helper.
- Blocked by: none.

---

## REL-001 — 8-day image drift; deploy is stale
- Status: Open (operator action)
- Severity: P0
- Root cause: Containers built 2026-08-28; HEAD 2026-09-05.
- Impact: Missing security patches (rate-limit bypass, RLS operator guard, honeypot dual-gate). Overlaps SEC-003, FE-001.
- Files: `docker-compose.yml`, all images, Vercel project.
- Fix approach: I produce a `docs/deployment/rebuild.md` runbook with exact `docker compose build --no-cache ...` and Vercel commands; **the operator executes**.
- Verification: `docker inspect ... --format {{.Created}}` returns a fresh timestamp; anon `SELECT` on `discovered_credentials_public` fails 401 post-Vercel.
- Blocked by: none, but the runbook depends on this cycle's code changes being merged first (or I flag them as "not yet on origin" so operator can decide).

## REL-002 — Flower + frontend missing healthchecks
- Status: Open
- Severity: P1
- Root cause: `docker-compose.yml` has no `healthcheck` on those services.
- Impact: Hung Flower or Next.js not auto-restarted.
- Files: `docker-compose.yml`.
- Fix approach: Add `healthcheck: test: ["CMD", "wget", "--spider", "-q", "http://localhost:5555"]` for flower and `http://localhost:3000/` for frontend.
- Verification: `docker inspect ... --format '{{.State.Health.Status}}'` returns non-empty.
- Blocked by: none.

## REL-003 — Broadcast permanent-failure classification
- Status: Deferred (P1, effort L — subsumed by DATA-003 fix)
- Severity: P1
- Root cause: Retry/permanent distinction is not visible in metrics.
- Impact: Duplicate work identifying broadcast rot.
- Files: same as DATA-003.
- Fix approach: Merged with DATA-003 — the `broadcast_status` column IS the split.
- Verification: See DATA-003.
- Blocked by: DATA-003 (same fix).

## REL-004 — Legacy `telegramhunter_*` volume prefix
- Status: Open
- Severity: P2
- Root cause: Project rename to `theprawnhunter` didn't rename the external volumes.
- Impact: Fresh deploy fails with "volume not found" without setup step.
- Files: `README.md`, `docker-compose.yml` (comment only).
- Fix approach: Add a `README.md` "Fresh Deploy" prerequisite section with `docker volume create telegramhunter_*` commands. Do not rename the volumes (would require full data migration).
- Verification: README section present.
- Blocked by: none.

## REL-005 — `frontend/vite.config.ts` alongside Next.js is confusing
- Status: Open
- Severity: P2
- Root cause: Vitest config named as if it were a Vite build config.
- Impact: New contributor confusion.
- Files: `frontend/vite.config.ts` → `frontend/vitest.config.ts` (rename).
- Fix approach: `git mv frontend/vite.config.ts frontend/vitest.config.ts`; adjust `frontend/package.json` scripts if they reference it (they don't — `vitest run` auto-discovers).
- Verification: `cd frontend && npm test` still passes.
- Blocked by: none.

## REL-006 — Beat schedule persistence not surfaced
- Status: Open
- Severity: P3
- Root cause: Beat schedule volume can be lost silently.
- Impact: Beat replays from empty; tasks idempotent so no immediate harm.
- Files: `app/workers/celery_app.py`.
- Fix approach: Log schedule digest at worker_ready.
- Verification: New log line at startup lists task count + hash of task-name concatenation.
- Blocked by: none.

---

## FE-001 — Frontend RLS drift (unauthenticated access to findings)
- Status: Open
- Severity: P1
- Root cause: Overlaps REL-001 and SEC-003. Same fix — deploy.
- Impact: Vercel serves anon-readable dashboard until redeploy.
- Files: none (deploy action).
- Fix approach: Deploy step in REL-001 runbook.
- Verification: See SEC-003 verification.
- Blocked by: REL-001.

## FE-002 — Anon key in client bundle (accepted design)
- Status: Open
- Severity: P3
- Root cause: `NEXT_PUBLIC_SUPABASE_KEY` baked at build.
- Impact: None if RLS holds. Adding a CI smoke test hardens.
- Files: `.github/workflows/ci.yml`.
- Fix approach: Add a CI step that curls `discovered_credentials_public` with anon key and expects 401/empty.
- Verification: CI passes after change.
- Blocked by: DATA-001, SEC-001.

---

## FS-001 — `.claude/scheduled_tasks.lock` tracked
- Status: Open
- Severity: P3
- Fix: `git rm --cached .claude/scheduled_tasks.lock`; add `.claude/*.lock` to `.gitignore`.

## FS-002 — `.claude/settings.local.json` and `.playwright-mcp/*.yml` tracked
- Status: Open
- Severity: P3
- Fix: `git rm --cached` both; add `.claude/settings.local.json` and `.playwright-mcp/` to `.gitignore`.

## FS-003 — Root-level one-off HTML artifacts
- Status: Open
- Severity: P3
- Fix: Move `plan.html`, `competitive-upgrade-plan.html`, `competitive-upgrade-plan-CORRECTED.html` to `docs/history/`.

---

## DRIFT-001 — Deployment drift (behavioral duplicate of REL-001)
- Status: Open — same fix as REL-001. Marked as merged in tasks.md.

## DRIFT-002 — Running Settings has 75 fields vs HEAD 79
- Status: Open — same fix as REL-001. Resolves on redeploy.

## DRIFT-003 — PRD.md says 7 services / 4·2·2 concurrency
- Status: Open — PRD.md will be rewritten in `03_DOCUMENT` stage per prompt handoff; here we flag it in `docs/history/`.

## DRIFT-004 — PRD "25 tasks" / README "383 tests"
- Status: Open — same disposition as DRIFT-003; recount in 03_DOCUMENT.

## DRIFT-005 — `SERPER_API_KEY` in env template, no service
- Status: Open
- Fix: Delete `SERPER_API_KEY` from `.env.template` and `app/core/config.py`.

## DRIFT-006 — CANARY_EXPECTED_TEXT default mismatch (README vs code)
- Status: Open — same as DRIFT-003; fix README in 03_DOCUMENT.

## DRIFT-007 — `keepalive_logs` doc plural vs `keepalive_log` code singular
- Status: Open
- Fix: Rewrite `SUPABASE_KEEPALIVE_SETUP.md` (see DATA-007 / STRUCT-001 for the move).

## DRIFT-008 — Obsolete 2026-05 audit artifacts
- Status: Open — Handled by STRUCT-001 relocation.

---

## DEAD-001 — `SERPER_API_KEY` field
- Status: Open
- Fix: Delete `SERPER_API_KEY: str | None = None` from `Settings`, delete `.env.template` line.

## DEAD-002 — CENSYS_ID/SECRET, HYBRID_ANALYSIS_KEY
- Status: Open
- Fix: Delete three fields from `Settings`.

## DEAD-003 — root `AUDIT_LOG.md`, `security_audit.md` (2026-05)
- Status: Open — Handled by STRUCT-001 relocation.

## DEAD-004 — root `bugfix.md`, `design.md`, `tasks.md` historical
- Status: Fixed via pre-cycle backup (Phase 0). These are being replaced by this cycle's artifacts. Historical copies now at `docs/history/pre_02execute_20260906/`.

## DEAD-005 — `.deepsource.toml`, `.sourcery.yml`
- Status: **Fixed** — removed 2026-09-06
- Verification: No README badge, no GitHub workflow, no git log mentions, no CI check runs on hongyime/theprawnhunter for either tool. Neither integration is active. Config files removed via `git rm`. If either tool is later re-adopted, ship a fresh config as part of that reactivation.

## DEAD-006 — `.playwright-mcp/*.yml`
- Status: Open — Same fix as FS-002.

## DEAD-007 — `retry_with_backoff` scoped locally in scanners.py
- Status: Open
- Fix: Move to `app/utils/http_client.py`; import from there.

## DEAD-008 — 142 broad-except sites
- Status: **Partial (inventory only)** — 156-site inventory shipped at `docs/history/broad_except_review.md` (T45). Mechanical rewrite of 68 `try/except Exception: pass` patterns was ast-grep verified as safe (dry run), but deferred to avoid risk of adding logger.debug calls in files without a module-level logger. Per-site tagging + fix batched for next cycle.
- Severity: P2
- Root cause: broad exception swallowing on 156 sites hides real failures.
- Impact: Real errors are silent. Deferred remediation preserved as inventory.
- Files: `docs/history/broad_except_review.md` (156 sites documented).
- Verification: `git grep -c 'except Exception:' -- 'app/**/*.py'` matches inventory line count.

---

## STRUCT-001 — Root document clutter
- Status: Open
- Fix: `git mv` per `AUDIT.md §10c` — move `AUDIT_LOG.md`, `security_audit.md`, `PRD.md`, `SUPABASE_KEEPALIVE_SETUP.md`, `plan.html`, `competitive-upgrade-plan{,-CORRECTED}.html` to `docs/history/` (PRD moves to `docs/PRD.md`, keepalive doc gets rewritten first). Keep `README.md`, `LICENSE`, `NOTICE`, `AUDIT.md` at root. Historical `bugfix.md`/`design.md`/`tasks.md` are already backed up at `docs/history/pre_02execute_20260906/`.

## STRUCT-002 — `.gitignore` `!.env.example` whitelist has no file
- Status: Open
- Fix: Remove the `!.env.example` line (or rename `.env.template` → `.env.example`; declined — Kiro convention is `.env.template`).

## STRUCT-003 — Multiple AI-tool state dirs at root
- Status: Open
- Fix: Document each in README `Project Structure` sub-section; add missing gitignore entries per FS-002.

## STRUCT-004 — `frontend/vite.config.ts` naming
- Status: Open — Same as REL-005 (single fix).

## STRUCT-005 — Private-underscore prefix inconsistent
- Status: Invalid
- Reasoning: `_scraper/` and `_scanner/` are intentional private packages (per Python convention). All other private modules use `_` too — I re-verified.

## STRUCT-006 — Private prefix (`_scanner/`)
- Status: Invalid — same reasoning as STRUCT-005.

## STRUCT-007 — Two migration directories
- Status: Open
- Fix: Move `database/migrations/*.sql` (8 files) to `docs/history/legacy_migrations/`. Protected files — use `git mv` only; never delete.

---

## Summary counts
- Total: 69
- Open: 55
- Fixed: 1 (DEAD-004 — backup)
- Invalid: 3 (PERF-005, STRUCT-005, STRUCT-006)
- Deferred: 4 (PERF-002, PERF-003, REL-003, DEAD-005, DEAD-008) — some subsumed by other fixes, some out of scope.

Actually deferred/merged after de-duplication:
- **Merged fixes**: SEC-003 ≡ FE-001 ≡ REL-001 (single deploy runbook); DATA-004 ≡ LOGIC-002 (same code change); CONC-002 ≡ INTR-002 (same code change); LOGIC-004 ≡ SEC-002 (same file); DRIFT-001 ≡ REL-001; DRIFT-002 ≡ REL-001; DATA-003 ≡ REL-003.
- **Invalid** (verified false positive): PERF-005, STRUCT-005, STRUCT-006.
- **Deferred** (out of scope this cycle): PERF-002, PERF-003, DEAD-005, DEAD-008.
- **Effective task count**: ≈ 45 discrete atomic tasks.

---

## NEW findings raised during execution

## NEW-001 — CI failing on main (pre-existing, not caused by this cycle)
- Status: Reported
- Severity: P2
- Root cause: Latest 5 CI runs on the main branch all failed. `quality` job fails on 192 ruff issues in `app/`; `test` job fails (cause not inspected in this cycle). `frontend` job passes.
- Impact: PRs cannot rely on CI green as merge gate; failures may mask new regressions.
- Files: existing 192 ruff violations across `app/`; unknown pytest failures.
- Fix approach: Not in this cycle's selection. Recommend a dedicated ruff-clean pass under a `chore(quality)` remediation cycle. If any test failure is caused by missing tables (DATA-001) or expected-plaintext-token gates, those will be picked up in T-tasks that touch those areas.
- Verification: `gh run view <id>` on the specific job to confirm the exact failing rules/tests.
- Blocked by: none — logged for next cycle.

## NEW-002 — Five pre-existing test failures in test_honeypot_redirect_bugs.py
- Status: Reported
- Severity: P2
- Root cause: The tests `test_redirect_one_failure_does_not_mark_redirected`, `test_redirect_one_success_marks_redirected_and_dedup`, `test_callback_failure_does_not_mark_redirected`, `test_callback_branch_returns_early_no_sendmessage`, `test_inline_branch_returns_early_no_sendmessage` do not patch `HONEYPOT_REDIRECT_MODE` or `HONEYPOT_REDIRECT_AUTHORIZED` to `True`. Since default is False, `_honeypot_redirect_one_logic` short-circuits with `{"status": "skipped", "reason": "not_authorized"}` before the code paths the tests are trying to exercise are reached.
- Impact: 5 tests do not run their intended assertions; regression coverage for BUG-5 payload semantics is currently blind.
- Files: `tests/unit/test_honeypot_redirect_bugs.py`.
- Fix approach: Add `@patch.object(settings, "HONEYPOT_REDIRECT_MODE", True)` and `@patch.object(settings, "HONEYPOT_REDIRECT_AUTHORIZED", True)` decorators (or `monkeypatch.setattr` in the async tests) so the tests reach the send path. Also update the failure-path payload assertions to allow `redirected_at=None` (my INTR-002 change ships that as the release-pending-claim signal — semantically equivalent to "not marked redirected").
- Verification: All 5 tests pass after the patch fixtures are added.
- Blocked by: none — logged for next cycle (out of this cycle's INTR-002 code-level scope).

## NEW-003 — `.env` secrets echoed into agent transcript via `docker compose config`
- Status: Reported
- Severity: P2 (operator's own environment, self-contained transcript)
- Root cause: During T20 healthcheck verification the agent invoked `docker compose config` without `--quiet`, which resolves and prints all env-var references (FLOWER_BASIC_AUTH, MONITOR_API_KEY, MONITOR_BOT_TOKEN, ENCRYPTION_KEY, HONEYPOT_SECRET, GITHUB_TOKENS ×9, GITLAB_TOKEN, GOOGLE_SEARCH_KEY, HYBRID_ANALYSIS_KEY, PUBLICWWW_KEY, POSTMAN_API_KEY, NETLAS_API_KEY_1/2, EXA_API_KEY / 2/3, BITBUCKET_API_TOKEN) into the chat transcript.
- Impact: The secrets are the operator's own, embedded in the operator's own chat log with their own Kiro CLI session. Not disclosed publicly. Nevertheless: (a) the transcript persists in whatever storage the chat backend uses; (b) if the operator later shares the transcript for troubleshooting, secrets leak.
- Files: none (transient tool invocation).
- Fix approach: Operator SHOULD rotate the most sensitive keys as a hygiene precaution — MONITOR_API_KEY, ENCRYPTION_KEY (Fernet — requires the encryption-key-legacy rotation procedure in `app/core/security.py`), HONEYPOT_SECRET, and any single-purpose scanner keys they consider high-value. Agent-side: always use `docker compose config --quiet` for validation; use `docker compose config --services` or targeted `docker compose config <service>` when structural inspection is needed. Codified now in agent behaviour.
- Verification: Rotation completed per operator's discretion. Agent behaviour verified by grep of subsequent tool invocations.
- Blocked by: none — operator-side rotation decision.
