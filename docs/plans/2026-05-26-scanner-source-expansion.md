# Scanner Source Expansion Plan

> **Goal:** Add 5 new/improved scanner sources to TheprawnHunter to recover the hit-rate lost from disabling Google CSE and GitLab.

**Architecture:** All scanners follow the existing pattern: Celery task → service class returns `[{token, chat_id?, meta}]` → enqueues to `validation` queue (already built last session). Each scanner gets a Redis cooldown on auth failures and runs on a stagger schedule.

**Tech Stack:** Python 3.11, Celery, httpx async, Redis cooldowns, regex extraction.

**Verification level:** Each task verified with `python -c "import ast; ast.parse(open(p).read())"` (parse bar) plus a live container smoke test triggering the scanner manually before moving on.

**Pattern A confirmed via mount inspection:** docker-compose has no `./app:/app` bind-mount — image is baked. Every code change requires `docker compose build worker-scanners` + `docker compose up -d --force-recreate worker-scanners` before runtime smoke tests.

---

## Task 1: Fix Pastebin scraping API or kill it

**Objective:** Pastebin scanner currently returns 0 every cycle ("IP not whitelisted"). Either whitelist or remove from beat.

**Files:**
- Modify: `app/workers/celery_app.py` (beat schedule)
- Modify: `app/services/scanners.py` (PastebinService — only if killing)

**Decision rule:** Pastebin Pro scraping API requires manual whitelist via pastebin.com/doc_scraping_api → the maintainer needs to either pay $30 once + whitelist Singapore IP, or accept dead scanner. Default action: **kill it**, since Exa now covers paste sites with fuller content extraction (no second-fetch needed).

**Step 1.1: Comment out beat entry**
```python
# scan-pastebin-12hours: DISABLED — Pastebin scraping API requires paid IP whitelist.
# Exa scanner (12h slot at minute=35) covers pastebin.com via includeDomains.
```
Replace lines `"scan-pastebin-12hours": { ... },` block.

**Step 1.2: Set Redis cooldown to silence any in-flight task**
```bash
docker exec theprawnhunter_redis redis-cli SET "cooldown:scanner:pastebin_api_broken" "disabled" EX 86400
```

**Step 1.3: Verify**
```bash
python -c "import ast; ast.parse(open('app/workers/celery_app.py').read()); print('ok')"
```
Expected: `ok`

**Step 1.4: Commit**
```
chore(scanners): disable Pastebin (paid IP whitelist required, Exa covers same domain)
```

---

## Task 2: Add PublicWWW to beat schedule

**Objective:** PublicWWW service class exists and `scan_publicwww` task exists, but no cron entry → never runs.

**Files:**
- Modify: `app/workers/celery_app.py`

**Pre-step:** Verify task exists.
```bash
grep -n "scan_publicwww" app/workers/tasks/scanner_tasks.py
```
Expected: function definition + `@app.task` line.

**Step 2.1: Add beat entry** — insert after `scan-bitbucket-8hours` block:
```python
"scan-publicwww-12hours": {
    "task": "scanner.scan_publicwww",
    "schedule": crontab(minute=15, hour="*/12"),
},
```
Slot picked: minute=15 (Pastebin's old slot, now free).

**Step 2.2: Verify env var configured**
```bash
grep PUBLICWWW_API_KEY .env
```
Expected: a line with the key. If missing → log warning to user (the maintainer must add it), but ship task anyway.

**Step 2.3: Verify**
```bash
python -c "import ast; ast.parse(open('app/workers/celery_app.py').read()); print('ok')"
```

**Step 2.4: Commit**
```
feat(scanners): schedule PublicWWW every 12h (HTML source code search)
```

---

## Task 3: Multi-token GitHub rotation

**Objective:** Single GitHub PAT is rate-limited to 30 req/min for code search. Pool 5 tokens → 5x throughput, smoother rate distribution = no more secondary rate limits.

**Files:**
- Modify: `app/services/scanners.py` (GithubService — token pool selection)
- Modify: `app/core/config.py` (add `GITHUB_TOKENS` list field)
- Modify: `.env` (the maintainer adds `GITHUB_TOKENS=` comma-separated list — manual, post-deploy)

**Architecture:** Round-robin token selection at request time, NOT at service init. Lets us add tokens without restart. Uses Redis INCR for distributed counter so multiple worker processes share rotation.

**Step 3.1: Add config field**

In `app/core/config.py`, find `GITHUB_TOKEN: Optional[str] = None` and add below it:
```python
# Multi-token rotation for GitHub code search.
# Comma-separated list of PATs. If set, overrides GITHUB_TOKEN.
# Each PAT gets its own 30 req/min budget — pool of 5 = 150 req/min total.
GITHUB_TOKENS: Optional[str] = None
```

**Step 3.2: Add token-pool helper**

In `app/services/scanners.py`, locate `class GithubService` and add at top of class:
```python
def _get_token(self) -> str:
    """
    Round-robin token selection from GITHUB_TOKENS env var (comma-separated).
    Falls back to single GITHUB_TOKEN if pool not configured.

    Uses Redis INCR for distributed round-robin so multiple worker processes
    share the rotation and don't all hammer token #0.
    """
    pool = settings.GITHUB_TOKENS
    if not pool:
        return settings.GITHUB_TOKEN or ""
    tokens = [t.strip() for t in pool.split(",") if t.strip()]
    if not tokens:
        return settings.GITHUB_TOKEN or ""
    if len(tokens) == 1:
        return tokens[0]

    # Distributed round-robin via Redis
    try:
        from app.workers.tasks.flow_tasks import redis_client
        idx = redis_client.incr("github_token_rotation") % len(tokens)
        return tokens[idx]
    except Exception:
        # Redis down — fall back to random selection
        import random
        return random.choice(tokens)
```

**Step 3.3: Use the helper in every API call**

In `GithubService.search_code()` (and any other GitHub HTTP call), replace:
```python
headers = {"Authorization": f"token {settings.GITHUB_TOKEN}"}
```
with:
```python
headers = {"Authorization": f"token {self._get_token()}"}
```

`search_files` to confirm exhaustive coverage:
```bash
grep -n "GITHUB_TOKEN" app/services/scanners.py
```
Replace each one (except the field reference itself in `_get_token`).

**Step 3.4: Verify**
```bash
python -c "import ast; ast.parse(open('app/services/scanners.py').read()); print('ok')"
python -c "import ast; ast.parse(open('app/core/config.py').read()); print('ok')"
```

**Step 3.5: Build & deploy**
```bash
docker compose build worker-scanners
docker compose up -d --force-recreate worker-scanners
```

**Step 3.6: Runtime smoke (single token, before the maintainer adds pool)**
```bash
docker exec theprawnhunter_worker-scanners celery -A app.workers.celery_app call scanner.scan_github
sleep 60
docker logs theprawnhunter_worker-scanners --tail 30 2>&1 | grep -iE "github|enqueue"
```
Expected: scanner runs, enqueues tokens (proves single-token fallback path works).

**Step 3.7: Commit**
```
feat(scanners): GitHub multi-token rotation via GITHUB_TOKENS env

Round-robin selection at request time using Redis INCR for distributed
counter across worker processes. Backwards-compatible: GITHUB_TOKEN still
works if GITHUB_TOKENS unset. Pool of 5 PATs = 5x rate limit headroom.
```

**Step 3.8: Operator handoff**
After commit, note: the maintainer must populate `GITHUB_TOKENS=ghp_X,ghp_Y,ghp_Z,...` in `.env` (comma-separated, no spaces) and restart worker-scanners. Each PAT must be from a separate GH account.

---

## Task 4: Wayback Machine scanner (CDX API)

**Objective:** Search the Internet Archive for historical URLs containing `api.telegram.org/bot` — paste URLs that 404 today often persist in Wayback. Massive backlog of leaked tokens.

**Files:**
- Create: nothing new — extend `app/services/scanners.py` with `WaybackService`
- Modify: `app/workers/tasks/scanner_tasks.py` (add `scan_wayback` task)
- Modify: `app/workers/celery_app.py` (add beat entry)

**API:** Wayback CDX API — `https://web.archive.org/cdx/search/cdx`. Free, no key. Rate limit ~1 req/sec courtesy. Returns timestamped snapshots.

**Approach:**
1. CDX query: `url=*api.telegram.org/bot*&matchType=prefix&output=json&limit=500`
2. For each snapshot, fetch the archived URL: `https://web.archive.org/web/{timestamp}/{original_url}`
3. Regex-extract token from the URL itself (often `bot<TOKEN>/sendMessage`) AND from the archived response body
4. Dedupe by URL hash to avoid re-fetching same snapshot

**Step 4.1: Add WaybackService class**

In `app/services/scanners.py`, after the `ExaService` class, add:
```python
class WaybackService:
    """
    Internet Archive Wayback Machine — historical URL scanner.

    Free, no API key. Uses CDX API for URL discovery + archived content fetch.
    Rate limit: ~1 req/sec courtesy (no documented hard cap; we sleep 1.2s).

    Token extraction strategy:
      1. From URL itself — many leaks are `.../bot<TOKEN>/sendMessage?...`
      2. From archived response body — paste content preserved in archive

    Dedup: SHA256 of original_url, cached in Redis 7 days. Avoids re-fetching
    same snapshot across runs.
    """

    CDX_URL = "https://web.archive.org/cdx/search/cdx"
    ARCHIVE_URL = "https://web.archive.org/web/{timestamp}/{url}"

    # Telegram bot token regex (same shape as elsewhere in codebase)
    TOKEN_REGEX = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")

    def __init__(self):
        self.timeout = httpx.Timeout(20.0, connect=10.0)
        self.dedupe_ttl = 7 * 86400  # 7 days

    async def search(self, query_pattern: str = "api.telegram.org/bot", limit: int = 500) -> list[dict]:
        """Query CDX, fetch unseen archived content, extract tokens."""
        from app.workers.tasks.flow_tasks import redis_client
        results = []

        # Step 1: CDX query — list snapshots matching the pattern
        params = {
            "url": f"*{query_pattern}*",
            "matchType": "prefix",
            "output": "json",
            "limit": limit,
            "filter": "statuscode:200",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(self.CDX_URL, params=params)
                resp.raise_for_status()
                rows = resp.json()
            except Exception as e:
                logger.error(f"[Wayback] CDX query failed: {e}")
                return []

            if not rows or len(rows) < 2:
                return []
            # First row is header: ["urlkey", "timestamp", "original", "mimetype", "statuscode", ...]
            header = rows[0]
            ts_idx = header.index("timestamp")
            url_idx = header.index("original")

            seen_in_run = set()
            for row in rows[1:]:
                if len(row) < max(ts_idx, url_idx) + 1:
                    continue
                timestamp = row[ts_idx]
                original = row[url_idx]

                # Dedupe by original URL (multiple snapshots = same content usually)
                url_hash = hashlib.sha256(original.encode()).hexdigest()[:16]
                if url_hash in seen_in_run:
                    continue
                seen_in_run.add(url_hash)

                redis_key = f"wayback:seen:{url_hash}"
                if redis_client.exists(redis_key):
                    continue

                # Step 2: Extract token from URL itself (cheap)
                url_tokens = self.TOKEN_REGEX.findall(original)
                for tok in url_tokens:
                    results.append({
                        "token": tok,
                        "meta": {
                            "wayback_url": original,
                            "wayback_timestamp": timestamp,
                            "extracted_from": "url",
                        }
                    })

                # Step 3: Fetch archived content for body extraction
                # Skip if URL itself yielded a token (likely body has more context)
                # Actually fetch always — body may have OTHER tokens
                archive_url = self.ARCHIVE_URL.format(timestamp=timestamp, url=original)
                try:
                    arc_resp = await client.get(archive_url, follow_redirects=True)
                    if arc_resp.status_code == 200:
                        body_tokens = set(self.TOKEN_REGEX.findall(arc_resp.text))
                        for tok in body_tokens:
                            if tok in url_tokens:
                                continue  # already added
                            results.append({
                                "token": tok,
                                "meta": {
                                    "wayback_url": original,
                                    "wayback_timestamp": timestamp,
                                    "extracted_from": "body",
                                }
                            })
                except Exception as e:
                    logger.debug(f"[Wayback] Fetch failed {original[:60]}: {e}")

                # Mark seen
                redis_client.setex(redis_key, self.dedupe_ttl, "1")

                # Courtesy rate limit
                await asyncio.sleep(1.2)

        logger.info(f"[Wayback] Returned {len(results)} matches across {len(seen_in_run)} snapshots")
        return results
```

Verify imports at top of `scanners.py` already include: `re`, `hashlib`, `httpx`, `asyncio`, `logger`. If `asyncio` missing, add it.

**Step 4.2: Add scanner task**

In `app/workers/tasks/scanner_tasks.py`:
- Import `WaybackService` in the existing import block:
```python
from app.services.scanners import (
    FofaService,
    GithubService,
    GitlabService,
    GrepAppService,
    PastebinService,
    ExaService,
    ShodanService,
    UrlScanService,
    WaybackService,  # NEW
)
```
- Instantiate at module level near other services:
```python
wayback_srv = WaybackService()
```
- Add task at end of file (before any `# ===` separator):
```python
@app.task(name="scanner.scan_wayback", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_wayback(query: str = None):
    return _run_sync(_scan_wayback_async(query))


async def _scan_wayback_async(query: str = None):
    """
    Wayback Machine historical URL scanner.

    No API key, no auth, no cooldown needed (CDX is free).
    1.2s inter-request sleep enforced inside WaybackService.search() per
    archive.org courtesy guidance.
    """
    logger.info("🔍 [Wayback] Starting historical URL scan...")

    if not _cb("wayback").allow():
        logger.warning("[Wayback] Circuit breaker OPEN — skipping run")
        return "Wayback skipped (circuit open)."

    try:
        # Default: search for telegram bot URLs in archive
        results = await wayback_srv.search(
            query_pattern=query or "api.telegram.org/bot",
            limit=int(os.getenv("WAYBACK_LIMIT", 500)),
        )
        _cb("wayback").record_success()

        if results:
            saved = await _save_credentials_async(results, "wayback")
            return f"Wayback scan finished. Enqueued {saved} tokens for validation."
        return "Wayback scan finished. 0 matches."
    except Exception as e:
        _cb("wayback").record_failure()
        logger.error(f"[Wayback] Error: {e}", exc_info=True)
        raise
```

**Step 4.3: Add beat entry**

In `app/workers/celery_app.py`, after `scan-exa-12hours`:
```python
"scan-wayback-24hours": {
    "task": "scanner.scan_wayback",
    "schedule": crontab(minute=0, hour=4),  # once daily at 04:00 UTC
},
```
Slot picked: 04:00 daily = quietest period, largest fetch footprint allowed.

**Step 4.4: Verify**
```bash
python -c "import ast; ast.parse(open('app/services/scanners.py').read()); print('scanners ok')"
python -c "import ast; ast.parse(open('app/workers/tasks/scanner_tasks.py').read()); print('tasks ok')"
python -c "import ast; ast.parse(open('app/workers/celery_app.py').read()); print('celery ok')"
```

**Step 4.5: Build & deploy**
```bash
docker compose build worker-scanners beat
docker compose up -d --force-recreate worker-scanners beat
```

**Step 4.6: Runtime smoke — limited probe**
```bash
docker exec theprawnhunter_worker-scanners python3 -c "
from app.services.scanners import WaybackService
import asyncio
async def main():
    s = WaybackService()
    r = await s.search(limit=5)
    print(f'count={len(r)}')
    for item in r[:3]:
        print(f\"  src={item['meta']['extracted_from']} ts={item['meta']['wayback_timestamp']}\")
asyncio.run(main())
"
```
Expected: returns count > 0 within ~30s.

**Step 4.7: Commit**
```
feat(scanners): Wayback Machine historical URL scanner

Queries archive.org CDX API for snapshots of api.telegram.org/bot URLs,
extracts tokens from both the URL itself and archived response bodies.
Free, no key, 1.2s inter-request courtesy sleep, 7-day Redis dedup.
Daily run at 04:00 UTC, default limit 500 snapshots/run.
```

---

## Task 5: Telegram MTProto self-search

**Objective:** Use the existing UserAgent (Telethon) sessions to search Telegram itself for `api.telegram.org/bot` mentions in public channels. Telegram's own search API — different result set than any web scanner. UserAgent already authenticated.

**Files:**
- Modify: `app/services/user_agent_srv.py` (add `search_messages` method)
- Modify: `app/workers/tasks/scanner_tasks.py` (add `scan_telegram_search` task)
- Modify: `app/workers/celery_app.py` (add beat entry)

**Caution:** Telegram per-account search rate limits are aggressive. We have 2 sessions. Search with one at a time, sleep 5s between queries, abort run if FloodWaitError fires.

**Step 5.1: Add search_messages method to user_agent_srv.py**

Read current state first:
```bash
grep -n "class UserAgentService" app/services/user_agent_srv.py
grep -n "def get_history" app/services/user_agent_srv.py
```

Add method to the same class as `get_history`:
```python
async def search_messages(self, query: str, limit: int = 100) -> list[dict]:
    """
    Global Telegram search via MTProto — searches across all chats the
    UserAgent has access to, plus public channel content.

    Uses telethon.tl.functions.messages.SearchGlobalRequest. Different from
    in-chat search — this hits Telegram's global index for public content.

    Rate limited: 5s sleep between calls, FloodWait sets 7200s session
    cooldown (same pattern as get_history).

    Returns list of {"text", "chat_id", "chat_name", "message_id", "date"}.
    """
    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import InputMessagesFilterEmpty
    from telethon.errors.rpcerrorlist import FloodWaitError

    if not self._client_pool:
        await self._init_client_pool()

    if not self._client_pool:
        logger.error("[UserAgent] No sessions available for search")
        return []

    # Pick first non-cooldown session
    client = None
    for c in self._client_pool:
        if not self._is_session_on_floodwait(c):
            client = c
            break

    if client is None:
        logger.warning("[UserAgent] All sessions on FloodWait cooldown — skipping search")
        return []

    results = []
    try:
        await asyncio.sleep(5.0)  # courtesy delay before search
        res = await client(SearchGlobalRequest(
            q=query,
            filter=InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=None,
            offset_id=0,
            limit=limit,
        ))

        for msg in res.messages or []:
            if not getattr(msg, "message", None):
                continue
            chat_id = None
            chat_name = None
            if hasattr(msg, "peer_id"):
                pid = msg.peer_id
                if hasattr(pid, "channel_id"):
                    chat_id = -1000000000000 - pid.channel_id  # standard supergroup id transform
                elif hasattr(pid, "chat_id"):
                    chat_id = -pid.chat_id
                elif hasattr(pid, "user_id"):
                    chat_id = pid.user_id

            # Try to enrich chat_name from chats list
            for chat in (res.chats or []):
                cid = getattr(chat, "id", None)
                if cid and chat_id and abs(cid) == abs(chat_id):
                    chat_name = getattr(chat, "title", None) or getattr(chat, "username", None)
                    break

            results.append({
                "text": msg.message,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "message_id": msg.id,
                "date": str(msg.date) if msg.date else None,
            })

        logger.info(f"[UserAgent] SearchGlobal('{query}') → {len(results)} messages")

    except FloodWaitError as e:
        wait = getattr(e, "seconds", 7200)
        logger.warning(f"[UserAgent] FloodWait on search: {wait}s — marking session cooldown")
        self._set_session_floodwait(client, wait)
        return []
    except Exception as e:
        logger.error(f"[UserAgent] search_messages failed: {e}", exc_info=True)
        return []

    return results
```

If `_is_session_on_floodwait` and `_set_session_floodwait` aren't already on the class (they should be from the earlier FloodWait fix), check:
```bash
grep -n "_is_session_on_floodwait\|_set_session_floodwait" app/services/user_agent_srv.py
```
If missing, port from `get_history` — same pattern.

**Step 5.2: Add scanner task**

In `app/workers/tasks/scanner_tasks.py`:
- Add module-level import:
```python
from app.services.user_agent_srv import UserAgentService
```
- Instantiate (or reuse if already exists):
```python
useragent_srv = UserAgentService()
```
- Add task:
```python
@app.task(name="scanner.scan_telegram_search", autoretry_for=(Exception,), retry_backoff=True, max_retries=1)
def scan_telegram_search(query: str = None):
    return _run_sync(_scan_telegram_search_async(query))


async def _scan_telegram_search_async(query: str = None):
    """
    Telegram MTProto global search — uses UserAgent session to query
    Telegram's own message index for token leaks in public channels.

    Different result space from any web scanner; catches leaks discussed
    in Telegram channels but never indexed by Google/Exa/etc.
    """
    logger.info("🔍 [TelegramSearch] Starting MTProto global search...")

    if not _cb("telegram_search").allow():
        logger.warning("[TelegramSearch] Circuit breaker OPEN — skipping")
        return "TelegramSearch skipped (circuit open)."

    queries = [query] if query else [
        "api.telegram.org/bot",
        "TELEGRAM_BOT_TOKEN",
        "bot_token telegram leaked",
    ]

    all_results = []
    try:
        from app.services.scanners import _is_valid_token
        token_re = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")

        for q in queries:
            messages = await useragent_srv.search_messages(q, limit=100)
            for msg in messages:
                text = msg.get("text", "")
                if not text:
                    continue
                tokens = token_re.findall(text)
                for tok in tokens:
                    if not _is_valid_token(tok):
                        continue
                    all_results.append({
                        "token": tok,
                        "chat_id": msg.get("chat_id"),
                        "meta": {
                            "telegram_chat_name": msg.get("chat_name"),
                            "telegram_message_id": msg.get("message_id"),
                            "telegram_date": msg.get("date"),
                            "telegram_query": q,
                        }
                    })
            # Inter-query sleep enforced by service (5s before each search)

        _cb("telegram_search").record_success()

        if all_results:
            saved = await _save_credentials_async(all_results, "telegram_search")
            return f"TelegramSearch finished. Enqueued {saved} tokens."
        return "TelegramSearch finished. 0 matches."

    except Exception as e:
        _cb("telegram_search").record_failure()
        logger.error(f"[TelegramSearch] Error: {e}", exc_info=True)
        raise
```

Verify `re` is imported at top of file (should be — used elsewhere).

**Step 5.3: Add beat entry**

In `app/workers/celery_app.py`:
```python
"scan-telegram-search-12hours": {
    "task": "scanner.scan_telegram_search",
    "schedule": crontab(minute=20, hour="*/12"),
},
```
Slot picked: minute=20 (between Exa at 35 and PublicWWW at 15). 12h cadence respects Telegram per-account quotas.

**Step 5.4: Verify**
```bash
python -c "import ast; ast.parse(open('app/services/user_agent_srv.py').read()); print('uag ok')"
python -c "import ast; ast.parse(open('app/workers/tasks/scanner_tasks.py').read()); print('tasks ok')"
python -c "import ast; ast.parse(open('app/workers/celery_app.py').read()); print('celery ok')"
```

**Step 5.5: Build & deploy**
```bash
docker compose build worker-scanners worker-scrape beat
docker compose up -d --force-recreate worker-scanners worker-scrape beat
```

(`worker-scrape` rebuilt because it imports `user_agent_srv`.)

**Step 5.6: Runtime smoke — single query, 10 result limit**
```bash
docker exec theprawnhunter_worker-scanners python3 -c "
import asyncio
from app.services.user_agent_srv import UserAgentService
async def main():
    s = UserAgentService()
    r = await s.search_messages('test', limit=5)
    print(f'returned {len(r)} messages')
    for m in r[:2]:
        print(f\"  chat={m.get('chat_name')} msg_len={len(m.get('text', ''))}\")
asyncio.run(main())
"
```
Expected: returns count ≥ 0 (depends on FloodWait state). If FloodWait → log warning, mark step skipped, continue.

**Step 5.7: Commit**
```
feat(scanners): Telegram MTProto self-search via UserAgent

Uses authenticated Telethon session to call SearchGlobalRequest against
Telegram's own message index. Catches leaks discussed in public channels
that never hit Google/Exa indices. 5s inter-query sleep + FloodWait
cooldown gating on session pool. 12h cadence respects per-account quotas.
```

---

## Final verification

After all 5 tasks committed:

```bash
# Show all 4 commits in order
cd /c/TheprawnHunter && git log --oneline -5

# Confirm beat schedule has new entries
docker exec theprawnhunter_beat celery -A app.workers.celery_app inspect scheduled 2>/dev/null | head -50
# OR
docker logs theprawnhunter_beat --tail 30 2>&1 | grep -iE "wayback|telegram_search|publicwww"
```

Push:
```bash
git push origin main
```

---

## Risk & rollback

Per task, on failure:
- `git revert HEAD` to undo the last commit (each task is one commit, so granular rollback works).
- Rebuild + recreate containers as in step N.5.

Across tasks: nothing destructive. No DB migrations, no state mutation. New tasks added to beat — disable by commenting out the beat entry, no orphaned data.

---

## Operator handoff (post-deploy)

the maintainer must do:
1. **Task 2**: confirm `PUBLICWWW_API_KEY` set in `.env` (warn if missing)
2. **Task 3**: populate `GITHUB_TOKENS=ghp_X,ghp_Y,...` in `.env` to activate rotation
3. **Task 5**: monitor FloodWait state — first run may eat the budget if queries trigger it

All other tasks self-activate from the next beat tick after deploy.

Machine-specific values in this document use privacy placeholders.
