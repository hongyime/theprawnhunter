"""Bot listener updates handlers."""

from .common import *

async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log a content-free update marker, skipping private and hub messages."""
    chat = update.effective_chat
    chat_id = chat.id if chat else None

    # SECURITY: never log private-DM contents. The /starthunter conversation
    # transports phone numbers, login codes, and 2FA passwords through private
    # DMs; if this handler logged them, they'd persist in container stdout logs.
    if chat and getattr(chat, "type", None) == "private":
        return

    # Suppress logging for messages originating from the monitor hub group.
    # The bot is a member of the hub so it receives every broadcast message back;
    # logging each one is pure noise and leaks message content into container logs.
    if chat_id is not None:
        from app.services.scraper_srv import _resolve_monitor_group_ids_async
        monitor_ids = await _resolve_monitor_group_ids_async()
        if str(chat_id) in monitor_ids or chat_id in monitor_ids:
            return

    user = update.effective_user
    message = update.message
    update_kind = "non_message"
    if message:
        if message.text:
            update_kind = "text"
        elif message.caption:
            update_kind = "caption"
        else:
            update_kind = "media"
    logger.info(
        "🔄 Update from subject=%s kind=%s",
        _subject_label(user.id) if user else "redacted",
        update_kind,
    )

async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-promote joining user-agent session accounts with MINIMAL admin rights.

    Fires whenever the bot sees a chat_member update in any chat. We only act
    when: (1) it's the monitor group, (2) the changed member matches a row in
    telegram_accounts.telegram_user_id, and (3) they just transitioned to
    'member' (i.e. joined).

    Permissions granted (all others explicitly False):
      • can_invite_users=True  — required so user_agent can InviteToChannel bots
    Permissions denied (nuke-protection):
      • can_change_info, can_delete_messages, can_restrict_members,
        can_pin_messages, can_promote_members, can_manage_video_chats
      • is_anonymous=False (stays visible as themselves in admin list)

    Note: Telegram's can_invite_users doesn't distinguish bot vs person invites.
    The membership audit task will detect and revert any non-bot user additions
    performed by session accounts.
    """
    cmu = update.chat_member
    if not cmu:
        return

    # Only care about the monitor group
    if cmu.chat.id != settings.MONITOR_GROUP_ID:
        return

    new_status = cmu.new_chat_member.status if cmu.new_chat_member else None
    old_status = cmu.old_chat_member.status if cmu.old_chat_member else None
    user = cmu.new_chat_member.user if cmu.new_chat_member else None
    if not user or user.is_bot:
        return  # bots are handled separately by user_agent.invite_bot_to_group
    subject = _subject_label(user.id)

    # Only act on transitions into 'member' state (join or unbanned)
    if new_status != "member" or old_status == "member":
        return

    # Match against telegram_accounts.telegram_user_id
    try:
        res = await asyncio.to_thread(
            lambda: db.table("telegram_accounts")
                .select("id, phone, status, is_admin_promoted")
                .eq("telegram_user_id", user.id)
                .limit(1)
                .execute()
        )
    except Exception as e:
        logger.debug(
            "[MemberJoin] db lookup failed for subject=%s: %s",
            subject,
            type(e).__name__,
        )
        return

    rows = res.data or []
    if not rows:
        logger.info(
            "[MemberJoin] subject=%s joined without a telegram_accounts row; "
            "treating as a regular guest and not promoting",
            subject,
        )
        return

    account = rows[0]
    if account.get("status") != "active":
        logger.warning(
            "[MemberJoin] subject=%s matches inactive account %s; not promoting",
            subject,
            account["id"],
        )
        return

    # SAFETY GUARD: if the user is ALREADY an admin/owner with broader rights,
    # never touch them. This protects the 4 legacy owner accounts + any human
    # you manually promoted with wider perms.
    try:
        current = await context.bot.get_chat_member(
            chat_id=settings.MONITOR_GROUP_ID,
            user_id=user.id,
        )
        current_status = getattr(current, "status", None)
        if current_status in ("creator", "administrator"):
            # Already has admin — preserve whatever perms they have
            logger.info(
                "[MemberJoin] subject=%s already has status=%s; leaving unchanged "
                "and marking account %s as promoted",
                subject,
                current_status,
                account["id"],
            )
            await asyncio.to_thread(
                lambda: db.table("telegram_accounts")
                    .update({
                        "is_admin_promoted": True,
                        "promoted_at": "now()",
                        "in_monitor_group": True,
                        "last_membership_check_at": "now()",
                    })
                    .eq("id", account["id"])
                    .execute()
            )
            return
    except Exception as e:
        logger.debug(
            "[MemberJoin] get_chat_member check failed for subject=%s (%s); "
            "proceeding with promotion attempt",
            subject,
            type(e).__name__,
        )

    # Promote with MINIMAL admin rights
    try:
        await context.bot.promote_chat_member(
            chat_id=settings.MONITOR_GROUP_ID,
            user_id=user.id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=True,          # REQUIRED for inviting bots via user session
            can_pin_messages=False,
            can_post_messages=False,
            can_edit_messages=False,
            is_anonymous=False,
        )
        # Mark promoted in DB
        await asyncio.to_thread(
            lambda: db.table("telegram_accounts")
                .update({
                    "is_admin_promoted": True,
                    "promoted_at": "now()",
                    "in_monitor_group": True,
                    "last_membership_check_at": "now()",
                })
                .eq("id", account["id"])
                .execute()
        )
        logger.info(
            "[MemberJoin] ✅ promoted subject=%s (account %s) with invite-only "
            "admin rights",
            subject,
            account["id"],
        )
    except Exception as e:
        logger.warning(
            "[MemberJoin] promote_chat_member failed for subject=%s: %s",
            subject,
            type(e).__name__,
        )
