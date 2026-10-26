"""Promote Telethon session accounts to anonymous admins in the monitor group.

Uses a user session with promote rights (the Bot API rejects promotion here
with ``Chat_admin_required`` and cannot set ``anonymous``). Every right is
granted, including anonymous posting, so new sessions behave exactly like the
existing ones.
"""

from .common import *

FULL_RIGHTS = {
    "change_info": True,
    "post_messages": True,
    "edit_messages": True,
    "delete_messages": True,
    "ban_users": True,
    "invite_users": True,
    "pin_messages": True,
    "add_admins": True,
    "anonymous": True,
    "manage_call": True,
    "other": True,
    "manage_topics": True,
}


@app.task(name="flow.ensure_session_admins")
def ensure_session_admins():
    return _run_sync(_ensure_session_admins_async())


def _client(path: str):
    from telethon import TelegramClient

    return TelegramClient(
        path.replace(".session", ""),
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
    )


async def _session_profiles() -> list[dict]:
    """Return session path, user id and current admin rights for each session."""
    import glob

    from telethon.tl.functions.channels import GetParticipantRequest

    profiles: list[dict] = []
    for path in sorted(glob.glob("/app/sessions/*.session")):
        client = _client(path)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                continue
            me = await client.get_me()
            entity = await client.get_entity(settings.MONITOR_GROUP_ID)
            part = (
                await client(GetParticipantRequest(channel=entity, participant=me.id))
            ).participant
            rights = getattr(part, "admin_rights", None)
            profiles.append(
                {
                    "path": path,
                    "user_id": me.id,
                    "username": me.username,
                    "participant": type(part).__name__,
                    "can_promote": bool(getattr(rights, "add_admins", False))
                    or type(part).__name__ == "ChannelParticipantCreator",
                    "complete": bool(
                        getattr(rights, "anonymous", False)
                        and getattr(rights, "post_messages", False)
                    ),
                }
            )
        except Exception as error:  # noqa: BLE001
            logger.debug(f"[SessionAdmins] probe skipped {path[-24:]}: {type(error).__name__}")
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return profiles


async def _ensure_session_admins_async() -> str:
    try:
        from telethon.tl.functions.channels import EditAdminRequest
        from telethon.tl.types import ChatAdminRights

        profiles = await _session_profiles()
        if not profiles:
            return "session_admins:no-sessions"

        promoters = sorted(
            [p for p in profiles if p["can_promote"]],
            key=lambda p: 0 if p["participant"] == "ChannelParticipantCreator" else 1,
        )
        if not promoters:
            return "session_admins:no-promoter"

        promoted = already = failed = 0
        for target in profiles:
            if target["complete"]:
                already += 1
                continue
            promoter = next(
                (p for p in promoters if p["user_id"] != target["user_id"]), None
            )
            if promoter is None:
                failed += 1
                continue
            client = _client(promoter["path"])
            try:
                await client.connect()
                entity = await client.get_entity(settings.MONITOR_GROUP_ID)
                target_entity = None
                async for participant in client.iter_participants(entity, limit=500):
                    if participant.id == target["user_id"]:
                        target_entity = participant
                        break
                if target_entity is None:
                    raise ValueError("target is not a member of the monitor group")
                await client(
                    EditAdminRequest(
                        channel=entity,
                        user_id=target_entity,
                        admin_rights=ChatAdminRights(**FULL_RIGHTS),
                        rank="",
                    )
                )
                promoted += 1
                logger.info(f"[SessionAdmins] promoted {target['username'] or target['user_id']}")
            except Exception as error:  # noqa: BLE001
                failed += 1
                logger.warning(
                    f"[SessionAdmins] could not promote {target['username'] or target['user_id']}: "
                    f"{type(error).__name__} {str(error)[:80]}"
                )
            finally:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass

        logger.info(f"[SessionAdmins] promoted={promoted} already={already} failed={failed}")
        return f"session_admins:promoted={promoted},already={already},failed={failed}"
    except Exception as error:  # noqa: BLE001 - never break the worker
        logger.warning(f"[SessionAdmins] aborted: {type(error).__name__}")
        return "session_admins:error"
