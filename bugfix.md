# Bug Fix Log

**Generated:** 2026-04-21  
**Source:** Multi-Agent Reconciliation Audit Report

---

## CRITICAL SEVERITY (Startup Crashes)

### BUG-001: GithubGistService Import Error
**Status:** Done (fixed 2026-08-29)
**Location:** `app/workers/tasks/scanner_tasks.py:17`  
**Root Cause:** `GithubGistService` is imported from `app.services.scanners` but is defined in `app.services.scanners_extension`  
**Impact:** Worker-scanners container crashes on startup with `ImportError`. All scanner tasks fail to load.  
**Fix:** Move import to `from app.services.scanners_extension import GithubGistService`

### BUG-002: PublicWWW Service Not Instantiated
**Status:** Done (fixed 2026-08-29)
**Location:** `app/workers/tasks/scanner_tasks.py:763`  
**Root Cause:** `scan_publicwww` task references `publicwww_srv` but this variable is never instantiated. Only `google_srv` and `netlas_srv` are instantiated from `scanners_extension`.  
**Impact:** `scan_publicwww` task raises `NameError` at runtime. Scheduled task fails every 6 hours.  
**Fix:** Add `publicwww_srv = PublicWwwService()` after line 46

### BUG-003: AuditLogger.log_event() Method Does Not Exist
**Status:** Done (fixed 2026-08-29)
**Location:** `app/workers/tasks/flow_tasks.py:24,25`  
**Root Cause:** Code calls `audit_log.log_event("exfiltrate.start", ...)` but `AuditLogger` only defines static `log()` method  
**Impact:** Every exfiltration task raises `AttributeError`. Enrichment and exfiltration pipeline broken.  
**Fix:** Replace `audit_log.log_event()` calls with `AuditLogger.log()` static method calls

---

## HIGH SEVERITY (Data Corruption / Security)

### BUG-004: Multi-Chat Meta Overwrite
**Status:** Open  
**Location:** `app/workers/tasks/flow_tasks.py:~line 180-190` (in `_enrich_logic`)  
**Root Cause:** When `len(chats) > 1`, second `update()` call sets `meta` to dict without `topic_id`, overwriting the `topic_id` just saved  
**Impact:** Any credential with multiple chats loses its `topic_id` immediately after enrichment. Broadcast fails for these credentials.  
**Fix:** Merge `topic_id` into the new meta dict before update: `meta["topic_id"] = thread_id` before the second update

### BUG-005: Monitor API Key Auth Bypass
**Status:** Open  
**Location:** `app/api/routers/monitor.py:18`  
**Root Cause:** `/monitor/stats` uses `_auth: None = Header(None, alias="X-Monitor-Key")` which does not correctly extract header value. Always evaluates to `None`.  
**Impact:** Authentication bypass. Anyone can access `/monitor/stats` regardless of `MONITOR_API_KEY` setting.  
**Fix:** Replace with `x_monitor_key: str | None = Header(None)` pattern used in other routes

### BUG-006: Raw Tokens Written to Database
**Status:** Open  
**Location:** `extension/background.js:~line 280`, `app/workers/tasks/flow_tasks.py:~line 50`  
**Root Cause:** Chrome extension writes plaintext tokens to DB. Self-heal path encrypts on first use, but window exists where plaintext tokens are in database.  
**Impact:** Any DB read before enrichment exposes plaintext bot tokens. Security vulnerability.  
**Fix:** Either encrypt client-side in extension OR add DB trigger to auto-encrypt on insert OR gate all reads through encryption check

### BUG-007: Synchronous DB Calls in Async Context
**Status:** Open  
**Location:** `app/workers/tasks/audit_tasks.py:~line 40-50`  
**Root Cause:** `_audit_active_topics_async()` calls `db.table(...).execute()` directly instead of via `async_execute()`  
**Impact:** Blocks event loop during DB I/O. Audit tasks hang under load.  
**Fix:** Wrap all `db.table()` calls with `await async_execute()`

---

## MEDIUM SEVERITY (Logic Errors)

### BUG-008: asyncio.run() Creates New Event Loop Per Task
**Status:** Open  
**Location:** Multiple task files using `_run_sync(coro)` helper  
**Root Cause:** Every Celery task uses `asyncio.run()` which creates new event loop. `BotClientManager._lock` is `asyncio.Lock` created at import time, belongs to old loop.  
**Impact:** Connection pooling defeated. `RuntimeError: Task got Future attached to a different loop` possible. Performance degradation.  
**Fix:** Refactor to use persistent event loop per worker process OR make tasks synchronous and use `loop.run_until_complete()` with shared loop

### BUG-009: Broadcast Pagination Missing
**Status:** Open  
**Location:** `app/workers/tasks/flow_tasks.py:~line 250` (in `_broadcast_logic`)  
**Root Cause:** Fetches only 20 messages per run with `.limit(20)`. No pagination loop.  
**Impact:** Throughput capped at 20 messages/hour (with 60-min schedule). Queue backlog grows unbounded.  
**Fix:** Add pagination loop or increase limit significantly

### BUG-010: Unbounded Enrichment Re-queuing
**Status:** Open  
**Location:** `app/workers/tasks/scanner_tasks.py:~line 150` (in `_save_credentials_async`)  
**Root Cause:** If token exists but has no chat_id, every scanner run calls `enrich_credential.delay()`. With 11 scanners, single stuck token generates unbounded tasks.  
**Impact:** Celery queue floods with duplicate enrichment tasks. Worker starvation.  
**Fix:** Add cooldown check before re-queuing enrichment (e.g., Redis key with TTL)

### BUG-011: BroadcasterService Instantiated Per Task
**Status:** Open  
**Location:** `app/workers/tasks/flow_tasks.py` (multiple functions)  
**Root Cause:** `BroadcasterService()` created inside every async function. Round-robin state (`_token_cycle`) lost between calls.  
**Impact:** Bot rotation does not work as intended. Same bot used repeatedly.  
**Fix:** Create module-level singleton or pass instance through task context

### BUG-012: Token Regex Inconsistency
**Status:** Open  
**Location:** `extension/content.js`, `app/utils/helpers.py`, `app/services/scanners.py`  
**Root Cause:** Extension uses 33-char pattern, helpers.py allows 33-35, scanners.py requires exactly 35. Validation inconsistent.  
**Impact:** Valid tokens rejected or invalid tokens accepted depending on code path.  
**Fix:** Standardize on 35-char secret requirement across all three locations

---

## LOW SEVERITY (Documentation / Cleanup)

### BUG-013: Invalid Scanner Sources in /scan/trigger
**Status:** Open  
**Location:** `app/api/routers/scan.py:~line 20`  
**Root Cause:** Accepts `censys` and `hybrid` as valid sources but no corresponding tasks exist  
**Impact:** Silent failure. Task-not-found error in Celery logs.  
**Fix:** Remove `censys` and `hybrid` from validation list

### BUG-014: LOCK_TTL_SECONDS Redefined
**Status:** Open  
**Location:** `app/services/bot_listener.py:38`  
**Root Cause:** Module redefines `LOCK_TTL_SECONDS = 120` after importing from `app.core.constants`  
**Impact:** Import on line 27 is dead code. Confusing for maintainers.  
**Fix:** Remove local redefinition, use imported constant

### BUG-015: Unused _verify_monitor_auth Helper
**Status:** Open  
**Location:** `app/api/routers/monitor.py:~line 10`  
**Root Cause:** Function defined but never called. Auth implemented inline in each route.  
**Impact:** Dead code. Maintenance confusion.  
**Fix:** Remove unused function OR refactor routes to use it

### BUG-016: Circuit Breaker Threshold Mismatch
**Status:** Open  
**Location:** `app/core/circuit_breaker.py:~line 150`  
**Root Cause:** PRD documents threshold=5, timeout=60s. Code sets threshold=3, timeout=300s.  
**Impact:** Documentation inaccuracy. Behavior differs from spec.  
**Fix:** Update PRD to match code OR update code to match PRD (decision needed)

---

## MISSING FEATURES (Acknowledged in Audit)

### MISSING-001: CSV Import Pipeline
**Status:** Done (implemented 2026-08-27 - see `app/workers/tasks/import_tasks.py`)
**Location:** `imports/` directory  
**Root Cause:** No Celery task processes `.pending` files  
**Impact:** Feature documented but non-functional  
**Fix:** Implement task OR remove documentation

### MISSING-002: Audit DB Persistence
**Status:** Open (Acknowledged)  
**Location:** `app/core/audit.py:68`  
**Root Cause:** `_persist_to_db()` is stub. No `audit_logs` table in schema.  
**Impact:** Audit events not persisted for compliance  
**Fix:** Create table and implement persistence OR document as future work

### MISSING-003: discover_chats() Method
**Status:** Open (Critical)  
**Location:** `app/services/scraper_srv.py`  
**Root Cause:** Method called in `_enrich_logic` but not visible in truncated file read  
**Impact:** Every enrichment task may fail if method is missing  
**Fix:** Verify method exists OR implement it

---

## CONFIGURATION GAPS

### CONFIG-001: EXTENSION_WRITE_SECRET Undocumented
**Status:** Open  
**Location:** `.env.template`, `README.md`  
**Root Cause:** Required for Chrome extension RLS policy but absent from env docs  
**Impact:** Extension cannot write to DB without manual DB configuration  
**Fix:** Add to `.env.template` and README env table

### CONFIG-002: BITBUCKET_APP_PASSWORD vs BITBUCKET_API_TOKEN
**Status:** Open  
**Location:** `app/core/config.py`, `README.md`  
**Root Cause:** Code uses `BITBUCKET_API_TOKEN`, docs reference `BITBUCKET_APP_PASSWORD`  
**Impact:** Configuration mismatch. Users set wrong variable.  
**Fix:** Update README to document `BITBUCKET_API_TOKEN`

### CONFIG-003: NETLAS_API_KEY_1/2 Undocumented
**Status:** Open  
**Location:** `README.md` scanner key table  
**Root Cause:** Two Netlas keys used with rotation but not in docs  
**Impact:** Users cannot configure Netlas scanner  
**Fix:** Add to README scanner key table

### CONFIG-004: TARGET_COUNTRIES Undocumented
**Status:** Open  
**Location:** `app/core/config.py`  
**Root Cause:** 50-country list used for rotation but not explained  
**Impact:** Users cannot customize country targeting  
**Fix:** Document in README optional config section

---

---

## ARCHITECTURAL ISSUES (Previously Deferred — NOW IN SCOPE)

### BUG-008: asyncio.run() Creates New Event Loop Per Task
**Status:** Open  
**Location:** All task files using `_run_sync(coro)` helper  
**Root Cause:** Every Celery task uses `asyncio.run()` which creates new event loop. `BotClientManager._lock` is `asyncio.Lock` created at import time, belongs to old loop.  
**Impact:** Connection pooling defeated. `RuntimeError: Task got Future attached to a different loop` possible. Performance degradation.  
**Fix Strategy:** Create persistent event loop per worker process using Celery worker signals. Replace `asyncio.run()` with `loop.run_until_complete()` using shared loop.

### BUG-011: BroadcasterService Instantiated Per Task
**Status:** Open  
**Location:** `app/workers/tasks/flow_tasks.py` (multiple functions)  
**Root Cause:** `BroadcasterService()` created inside every async function. Round-robin state (`_token_cycle`) lost between calls.  
**Impact:** Bot rotation does not work as intended. Same bot used repeatedly.  
**Fix Strategy:** Create module-level singleton instance. Pass through task context or use global.

### BUG-006: Raw Tokens Written to Database
**Status:** Open  
**Location:** `extension/background.js:~line 280`, `app/workers/tasks/flow_tasks.py:~line 50`  
**Root Cause:** Chrome extension writes plaintext tokens to DB. Self-heal path encrypts on first use, but window exists where plaintext tokens are in database.  
**Impact:** Any DB read before enrichment exposes plaintext bot tokens. Security vulnerability.  
**Fix Strategy:** Add client-side encryption in extension using Web Crypto API. Encrypt before Supabase write. Backend detects Fernet format and skips self-heal.

### BUG-016: Circuit Breaker Threshold Mismatch
**Status:** Open  
**Location:** `app/core/circuit_breaker.py:~line 150`  
**Root Cause:** PRD documents threshold=5, timeout=60s. Code sets threshold=3, timeout=300s.  
**Impact:** Documentation inaccuracy. Behavior differs from spec.  
**Fix Strategy:** Update code to match PRD: `failure_threshold=5`, `recovery_timeout=60`. More lenient thresholds reduce false positives.

---

## MISSING FEATURES (Previously Deferred — NOW IN SCOPE)

### MISSING-001: CSV Import Pipeline
**Status:** Open  
**Location:** `imports/` directory, `imports/README.md`  
**Root Cause:** No Celery task processes `.pending` files  
**Impact:** Feature documented but non-functional  
**Fix Strategy:** Implement `system.import_csv` Celery task. Scan `imports/` for `.csv` files, validate format, insert to DB, rename to `.processed`.

### MISSING-002: Audit DB Persistence
**Status:** Open  
**Location:** `app/core/audit.py:68`, `database/init.sql`  
**Root Cause:** `_persist_to_db()` is stub. No `audit_logs` table in schema.  
**Impact:** Audit events not persisted for compliance  
**Fix Strategy:** Create `audit_logs` table in `init.sql`. Implement `_persist_to_db()` to insert high-importance events.

---

## TOTAL: 20 Bugs + 3 Missing Features + 4 Config Gaps = 27 Items
**ALL ITEMS NOW IN SCOPE FOR THIS CYCLE**
