"""Promote Telethon session accounts to full admins in the monitor group."""

from .common import *

PROMOTE_FLAGS = {
    "can_manage_chat": True,
    "can_change_info": True,
    "can_delete_messages": True,
    "can_invite_users": True,
    "can_restrict_members": True,
    "can_pin_messages": True,
    "can_promote_members": True,
    "can_manage_topics": True,
    "can_manage_video_chats": True,
    "can_post_stories": True,
    "can_edit_stories": True,
    "can_delete_stories": True,
}


@app.task(name="flow.ensure_session_admins")
def ensure_session_admins():
    return _run_sync(_ensure_session_admins_async())


async def _session_user_ids() -> list[tuple[str, int, str | None]]:
    import glob

    from telethon import TelegramClient

    found: list[tuple[str, int, str | None]] = []
    for path in sorted(glob.glob("/app/sessions/*.session")):
        client = TelegramClient(
            path.replace(".session", ""),
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                continue
            me = await client.get_me()
            found.append((path, me.id, me.username))
        except Exception as error:  # noqa: BLE001
            logger.debug(f"[SessionAdmins] skipped {path[-24:]}: {type(error).__name__}")
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return found


async def _ensure_session_admins_async() -> str:
    try:
        from telegram import Bot

        if not settings.bot_tokens:
            return "session_admins:no-bot-token"

        sessions = await _session_user_ids()
        if not sessions:
            return "session_admins:no-sessions"

        bot = Bot(settings.bot_tokens[0])
        promoted = already = failed = 0
        async with bot:
            for path, user_id, username in sessions:
                label = username or str(user_id)
                try:
                    member = await bot.get_chat_member(settings.MONITOR_GROUP_ID, user_id)
                    rights = getattr(member, "can_post_messages", None)
                    status = str(getattr(member, "status", ""))
                    if status.endswith("ADMINISTRATOR") and rights:
                        already += 1
                        continue
                    await bot.promote_chat_member(
                        chat_id=settings.MONITOR_GROUP_ID,
                        user_id=user_id,
                        **PROMOTE_FLAGS,
                    )
                    promoted += 1
                    logger.info(f"[SessionAdmins] promoted {label} to admin")
                except Exception as error:  # noqa: BLE001
                    failed += 1
                    logger.warning(
                        f"[SessionAdmins] could not promote {label}: {type(error).__name__}"
                    )
        logger.info(
            f"[SessionAdmins] promoted={promoted} already={already} failed={failed}"
        )
        return f"session_admins:promoted={promoted},already={already},failed={failed}"
    except Exception as error:  # noqa: BLE001 - never break the worker
        logger.warning(f"[SessionAdmins] aborted: {type(error).__name__}")
        return "session_admins:error"
