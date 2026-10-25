"""User-agent group and archive operations."""

import logging

from app.services.engagement import pseudonymize_engagement_subject

from .common import *
from .session_lifecycle import UserAgentSessionUnavailableError


def _subject_label(user_id: object) -> str:
    try:
        return pseudonymize_engagement_subject(user_id)[:12]
    except Exception:
        return "redacted"


def _normalise_topic_title(value: object) -> str:
    return " ".join(str(value or "").strip().lstrip("@").lower().split())


class UserAgentGroupArchive:
    async def invite_bot_to_group(self, bot_username: str, group_id: int | str) -> bool:
        """
        Invites a bot to the specified group (chat/channel).
        """
        async with self.lock:
            if not await self.start():
                return False

            try:
                bot_entity = await self.client.get_entity(bot_username)
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                group_entity = await self.client.get_entity(target)

                logger.info(f"    🚀 [UserAgent] Inviting {bot_username} to group...")
                from telethon.tl.functions.channels import InviteToChannelRequest
                from telethon.tl.functions.messages import AddChatUserRequest

                try:
                    await self.client(InviteToChannelRequest(channel=group_entity, users=[bot_entity]))
                    logger.info("    ✅ [UserAgent] Invite successful (Channel/Supergroup).")
                    return True
                except errors.UserAlreadyParticipantError:
                    logger.info("    ℹ️ [UserAgent] Bot is already inside the group.")
                    return True
                except errors.FloodWaitError:
                    raise
                except Exception as e_channel:
                    if _is_terminal_invite_error(e_channel):
                        logger.error(f"    ❌ [UserAgent] Terminal invite failure: {e_channel}")
                        return False
                    logger.debug(f"    ℹ️ [UserAgent] InviteToChannelRequest skipped ({e_channel}), trying AddChatUserRequest...")
                    try:
                        await self.client(AddChatUserRequest(chat_id=group_entity.id, user_id=bot_entity, fwd_limit=0))
                        logger.info("    ✅ [UserAgent] Invite successful (Basic Chat).")
                        return True
                    except errors.UserAlreadyParticipantError:
                        logger.info("    ℹ️ [UserAgent] Bot is already inside the group.")
                        return True
                    except errors.FloodWaitError:
                        raise
                    except Exception as e_chat:
                        logger.error(f"    ❌ [UserAgent] Invite failed (Channel err: {e_channel} | Chat err: {e_chat})")
                        return False
            except errors.FloodWaitError as e:
                await self._handle_flood_error(e)
                return False
            except Exception as e:
                logger.error(f"    ❌ [UserAgent] Error: {e}")
                return False
            finally:
                await self._disconnect()

    async def kick_bot_from_group(self, bot_username: str, group_id: int | str) -> bool:
        """
        Kicks/bans then unbans a bot from the monitor group.
        Used after Matkap-style forwarding to remove the victim bot
        so it cannot see further group messages (OPSEC cleanup).
        """
        async with self.lock:
            if not await self.start():
                return False
            try:
                bot_entity = await self.client.get_entity(bot_username)
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                group_entity = await self.client.get_entity(target)

                from datetime import datetime, timedelta

                from telethon.tl.functions.channels import EditBannedRequest
                from telethon.tl.types import ChatBannedRights

                # Ban (kicks non-admin bots immediately)
                await self.client(EditBannedRequest(
                    channel=group_entity,
                    participant=bot_entity,
                    banned_rights=ChatBannedRights(
                        until_date=datetime.now(UTC) + timedelta(seconds=30),
                        view_messages=True,
                    )
                ))
                # Unban so the bot can be re-invited in the future if needed
                await self.client(EditBannedRequest(
                    channel=group_entity,
                    participant=bot_entity,
                    banned_rights=ChatBannedRights(until_date=None)
                ))
                logger.info(f"    ✅ [UserAgent] Kicked @{bot_username} from group (ban+unban).")
                return True
            except errors.FloodWaitError as e:
                await self._handle_flood_error(e)
                return False
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] kick_bot_from_group failed for @{bot_username}: {e}")
                return False
            finally:
                await self._disconnect()

    async def _handle_flood_error(self, e):
        """Logs and sets persistent cooldown for FloodWaitError (Per Session)."""
        from app.core.redis_srv import redis_srv
        wait_seconds = e.seconds
        current_session = getattr(self, 'current_session_name', 'unknown')
        # Must use the same namespace as the cooldown check in start()
        cooldown_key = f"user_agent:cooldown:{current_session}"
        if wait_seconds > 300:
            logger.warning(f"\n🛑 [UserAgent] SEVERE FLOOD WAIT for '{current_session}': {wait_seconds}s.")
            redis_srv.set_cooldown(cooldown_key, wait_seconds + 60)
        else:
            logger.warning(f"    🛑 [UserAgent] FLOOD WAIT for '{current_session}': {wait_seconds}s.")
            redis_srv.set_cooldown(cooldown_key, wait_seconds + 10)

    async def find_topic_id(self, group_id: int | str, topic_name: str) -> int | None:
        async with self.lock:
            if not await self.start():
                raise UserAgentSessionUnavailableError(
                    "No usable Telegram user session is available to resolve forum topics."
                )
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                from telethon.tl.functions.messages import GetForumTopicsRequest

                query = topic_name.strip().lstrip("@")
                res = await self.client(
                    GetForumTopicsRequest(
                        peer=entity,
                        q=query,
                        offset_date=0,
                        offset_id=0,
                        offset_topic=0,
                        limit=20,
                    )
                )
                wanted = _normalise_topic_title(topic_name)
                if res.topics:
                    for topic in res.topics:
                        if topic.title == topic_name:
                            logger.info(f"    🔍 [UserAgent] Found existing topic: {topic.title} ({topic.id})")
                            return topic.id
                    for topic in res.topics:
                        if _normalise_topic_title(topic.title) == wanted:
                            logger.info(
                                f"    🔍 [UserAgent] Matched topic by normalised title: {topic.title} ({topic.id})"
                            )
                            return topic.id
                return None
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] Find topic failed: {e}")
                raise
            finally:
                await self._disconnect()

    async def get_topic_title(self, group_id: int | str, topic_id: int | str) -> str | None:
        """Return a forum topic's title, or ``None`` when the topic is absent."""
        async with self.lock:
            if not await self.start():
                raise UserAgentSessionUnavailableError(
                    "No usable Telegram user session is available to inspect forum topics."
                )
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                from telethon.tl.functions.messages import GetForumTopicsByIDRequest

                topic_id_int = int(topic_id)
                result = await self.client(
                    GetForumTopicsByIDRequest(peer=entity, topics=[topic_id_int])
                )
                for topic in getattr(result, "topics", []) or []:
                    if int(getattr(topic, "id", -1)) == topic_id_int:
                        return getattr(topic, "title", None)
                return None
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] Topic lookup failed for {topic_id}: {e}")
                raise
            finally:
                await self._disconnect()

    async def mute_topic(
        self,
        group_id: int | str,
        topic_id: int | str,
        *,
        mute_until: datetime | None = None,
    ) -> bool:
        try:
            topic_id_int = int(topic_id)
        except (TypeError, ValueError):
            return False
        if topic_id_int <= 1:
            return False

        async with self.lock:
            if not await self.start():
                return False
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                from telethon.tl.functions.account import UpdateNotifySettingsRequest
                from telethon.tl.types import InputNotifyForumTopic, InputPeerNotifySettings

                await self.client(
                    UpdateNotifySettingsRequest(
                        peer=InputNotifyForumTopic(peer=entity, top_msg_id=topic_id_int),
                        settings=InputPeerNotifySettings(
                            mute_until=mute_until or datetime.now(UTC) + timedelta(days=3650)
                        ),
                    )
                )
                return True
            except errors.FloodWaitError as e:
                await self._handle_flood_error(e)
                return False
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] Mute topic {topic_id} failed: {e}")
                return False
            finally:
                await self._disconnect()

    async def mute_all_topics(
        self,
        group_id: int | str,
        *,
        mute_until: datetime | None = None,
    ) -> int:
        async with self.lock:
            if not await self.start():
                return 0
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                from telethon.tl.functions.account import UpdateNotifySettingsRequest
                from telethon.tl.functions.messages import GetForumTopicsRequest
                from telethon.tl.types import (
                    InputNotifyForumTopic,
                    InputNotifyPeer,
                    InputPeerNotifySettings,
                )

                notification_settings = InputPeerNotifySettings(
                    mute_until=mute_until or datetime.now(UTC) + timedelta(days=3650)
                )
                await self.client(
                    UpdateNotifySettingsRequest(
                        peer=InputNotifyPeer(peer=entity),
                        settings=notification_settings,
                    )
                )

                muted = 0
                offset_topic = 0
                seen: set[int] = set()
                while True:
                    result = await self.client(
                        GetForumTopicsRequest(
                            peer=entity,
                            offset_date=0,
                            offset_id=0,
                            offset_topic=offset_topic,
                            limit=100,
                            q=None,
                        )
                    )
                    topics = list(getattr(result, "topics", []) or [])
                    if not topics:
                        break
                    for topic in topics:
                        topic_id = int(getattr(topic, "id", 0) or 0)
                        if topic_id <= 1 or topic_id in seen:
                            continue
                        seen.add(topic_id)
                        await self.client(
                            UpdateNotifySettingsRequest(
                                peer=InputNotifyForumTopic(
                                    peer=entity,
                                    top_msg_id=topic_id,
                                ),
                                settings=notification_settings,
                            )
                        )
                        muted += 1
                    if len(topics) < 100:
                        break
                    next_offset = int(getattr(topics[-1], "id", 0) or 0)
                    if next_offset <= 0 or next_offset == offset_topic:
                        break
                    offset_topic = next_offset
                return muted
            except errors.FloodWaitError as e:
                await self._handle_flood_error(e)
                return 0
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] Mute-all topics failed: {e}")
                return 0
            finally:
                await self._disconnect()

    async def check_membership(self, group_id: int | str, user_identifier: str | int) -> dict | None:
        async with self.lock:
            if not await self.start():
                return None
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                group_entity = await self.client.get_entity(target)
                if str(user_identifier).lstrip('-').isdigit():
                    user_target = int(user_identifier)
                else:
                    user_target = user_identifier
                try:
                    user_entity = await self.client.get_entity(user_target)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")
                    return None
                from telethon.tl.functions.channels import GetParticipantRequest
                try:
                    result = await self.client(GetParticipantRequest(channel=group_entity, participant=user_entity))
                    return {
                        "id": getattr(user_entity, 'id', 0),
                        "username": getattr(user_entity, 'username', None),
                        "is_admin": hasattr(result.participant, 'admin_rights') and result.participant.admin_rights is not None
                    }
                except Exception as e:
                    if "USER_NOT_PARTICIPANT" in str(e) or "400" in str(e):
                        return None
                    return None
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")
                return None
            finally:
                await self._disconnect()

    async def promote_to_admin(self, group_id: int | str, user_identifier: str | int, title: str = "Admin", anonymous: bool = True) -> bool:
        async with self.lock:
            if not await self.start():
                return False
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                group_entity = await self.client.get_entity(target)
                if str(user_identifier).lstrip('-').isdigit():
                    user_target = int(user_identifier)
                else:
                    user_target = user_identifier
                user_entity = await self.client.get_entity(user_target)
                from telethon.tl.functions.channels import EditAdminRequest
                from telethon.tl.types import ChatAdminRights
                admin_rights = ChatAdminRights(
                    change_info=True, post_messages=True, edit_messages=True, delete_messages=True,
                    ban_users=True, invite_users=True, pin_messages=True, manage_call=True,
                    other=True, manage_topics=True, anonymous=anonymous
                )
                await self.client(EditAdminRequest(channel=group_entity, user_id=user_entity, admin_rights=admin_rights, rank=title))
                logger.info(
                    "    👑 [UserAgent] Promoted account to admin (anon=%s) in group.",
                    anonymous,
                )
                return True
            except errors.FloodWaitError as e:
                await self._handle_flood_error(e)
                return False
            except Exception as e:
                logger.error(
                    "    ❌ [UserAgent] Promote failed: %s",
                    type(e).__name__,
                )
                return False
            finally:
                await self._disconnect()

    async def _ensure_monitor_bots_membership(self):
        """Checks and ensures all broadcaster bots and user accounts are in the monitor group."""
        try:
            tokens = settings.bot_tokens
            group_id = settings.MONITOR_GROUP_ID
            if not tokens or not group_id:
                return
            logger.info("    🐶 [UserAgent] Syncing Hub memberships and permissions...")
            for token in tokens:
                try:
                    bot_id = int(token.split(':')[0])
                    member = await self.check_membership(group_id, bot_id)
                    if not member:
                        from telegram import Bot
                        temp_bot = Bot(token)
                        me = await temp_bot.get_me()
                        if await self.invite_bot_to_group(me.username, group_id):
                            await self.promote_to_admin(group_id, me.username, anonymous=False)
                    elif not member.get("is_admin"):
                        await self.promote_to_admin(group_id, bot_id, anonymous=False)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")
            if not self.sessions:
                self._discover_sessions()
            for session_path in self.sessions:
                if not await self._connect_to_session(session_path):
                    continue
                try:
                    me = await self.client.get_me()
                    await self._disconnect()
                    member = await self.check_membership(group_id, me.id)
                    if member:
                        if not member.get("is_admin"):
                            await self.promote_to_admin(group_id, me.id, anonymous=True)
                    else:
                        logger.warning(f"    ⚠️ User @{me.username} is NOT in Hub. Please add manually.")
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")
        except Exception as e:
            logger.error(f"    ❌ [UserAgent] Membership sync fatal error: {e}")

    async def send_message(self, target: int | str, message: str, thread_id: int | None = None) -> bool:
        """Sends a text message to a target (group/user) as the User Agent."""
        async with self.lock:
            if not await self.start():
                return False
            try:
                entity = int(target) if str(target).lstrip("-").isdigit() else target
                await self.client.send_message(entity, message, reply_to=thread_id)
                logger.info(f"    🗣️ [UserAgent] Sent (session={self.current_session_name}): '{message[:30]}...'")
                return True
            except Exception as e:
                logger.error(f"    ❌ [UserAgent] Send failed: {e}")
                return False
            finally:
                await self._disconnect()

    @staticmethod
    def _coerce_chat_ref(value: int | str) -> int | str:
        return int(value) if str(value).lstrip("-").isdigit() else value

    @staticmethod
    def _archive_temp_path(message, message_id: int) -> str:
        original_name = os.path.basename(str(getattr(getattr(message, "file", None), "name", "") or ""))
        filename = f"{ARCHIVE_TMP_PREFIX}{uuid.uuid4().hex[:8]}_{message_id}"
        filename = f"{filename}_{original_name}" if original_name else f"{filename}.bin"
        archive_dir = ARCHIVE_TMP_DIR.rstrip("/\\")
        return f"{archive_dir}/{filename}"

    @staticmethod
    def _archive_size_result(message) -> ArchiveMediaResult | None:
        size_bytes = getattr(getattr(message, "file", None), "size", None)
        if size_bytes is None:
            return None

        max_bytes = settings.MAX_ARCHIVE_SIZE_MB * 1024 * 1024
        if size_bytes <= max_bytes:
            return None

        size_mb = size_bytes / 1024 / 1024
        detail = (
            f"Attachment is {size_mb:.1f} MB; "
            f"limit is {settings.MAX_ARCHIVE_SIZE_MB} MB."
        )
        return ArchiveMediaResult(
            ok=False,
            code="too_large",
            detail=detail,
            size_bytes=size_bytes,
        )

    @staticmethod
    def _should_retry_missing_entity(source: int | str, exc: Exception) -> bool:
        return (
            "Could not find the input entity" in str(exc)
            or str(source).lstrip("-").isdigit()
        )

    async def _resolve_archive_username_from_credentials(self, source: int | str) -> str | None:
        lookup_filter = _credential_lookup_filter_for_source(source)
        if not lookup_filter:
            return None

        try:
            from app.core.database import db

            res = await _async_execute(
                db.table("discovered_credentials")
                .select("meta")
                .or_(lookup_filter)
                .limit(1)
            )
        except Exception as exc:
            logger.warning(
                f"    ⚠️ [UserAgent] Failed credential username lookup for source={source}: {exc}"
            )
            return None

        for row in res.data or []:
            username = _username_from_meta(row.get("meta"))
            if username:
                return username
        return None

    async def _get_archive_message_with_entity_fallback(
        self,
        source: int | str,
        message_id: int,
        *,
        exact_source: bool = False,
    ):
        try:
            return await self.client.get_messages(source, ids=message_id)
        except (ValueError, TypeError) as entity_err:
            if exact_source:
                return ArchiveMediaResult(
                    ok=False,
                    code="missing_access_hash",
                    detail="The source chat is not accessible to this user session.",
                )
            if not self._should_retry_missing_entity(source, entity_err):
                raise

            username_source = await self._resolve_archive_username_from_credentials(source)
            if not username_source:
                detail = (
                    f"Missing Telethon access_hash for raw source {source}; "
                    "no discovered_credentials.meta.bot_username fallback was found."
                )
                logger.warning(f"    ⚠️ [UserAgent] {detail}")
                return ArchiveMediaResult(ok=False, code="missing_access_hash", detail=detail)

            logger.info(
                f"    ℹ️ [UserAgent] Retrying archive source {source} as {username_source}"
            )
            try:
                return await self.client.get_messages(username_source, ids=message_id)
            except (ValueError, TypeError) as retry_err:
                detail = (
                    f"Resolved {source} to {username_source}, but Telethon still could not "
                    f"load msg={message_id}: {retry_err}"
                )
                logger.warning(f"    ⚠️ [UserAgent] {detail}")
                return ArchiveMediaResult(ok=False, code="missing_access_hash", detail=detail[:200])

    async def _send_archived_file_with_retries(
        self,
        target_chat_id: int | str,
        temp_path: str,
        topic_id: int | None,
        caption: str,
    ) -> ArchiveMediaResult:
        attempts = max(1, settings.ARCHIVE_RETRY_ATTEMPTS)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                await asyncio.wait_for(
                    self.client.send_file(
                        target_chat_id,
                        temp_path,
                        caption=(caption or "")[:1024],
                        reply_to=topic_id if topic_id != 1 else None,
                    ),
                    timeout=settings.ARCHIVE_UPLOAD_TIMEOUT_SECONDS,
                )
                return ArchiveMediaResult(ok=True)
            except TimeoutError as e:
                last_error = e
                code = "timeout"
            except Exception as e:
                last_error = e
                code = "upload_failed"

            if attempt + 1 < attempts:
                await asyncio.sleep(settings.ARCHIVE_RETRY_BACKOFF_SECONDS * (2 ** attempt))

        detail = str(last_error)[:200] if last_error else "Upload failed."
        if not detail and isinstance(last_error, asyncio.TimeoutError):
            detail = f"Upload exceeded {settings.ARCHIVE_UPLOAD_TIMEOUT_SECONDS}s."
        return ArchiveMediaResult(ok=False, code=code, detail=detail)

    async def archive_media_transiently(
        self,
        entity_or_chat_id: int | str,
        message_id: int,
        target_chat_id: int | str,
        topic_id: int | None = None,
        caption: str = "",
        *,
        exact_source: bool = False,
    ) -> ArchiveMediaResult:
        """
        Download a source attachment to a temporary file, re-upload it, then
        immediately remove the local file regardless of upload outcome.
        """
        if message_id <= 0:
            return ArchiveMediaResult(
                ok=False, code="invalid_source", detail="A positive source message ID is required."
            )
        temp_path = ""
        async with self.lock:
            if not await self.start():
                return ArchiveMediaResult(ok=False, code="session_unavailable", detail="No usable user session.")
            try:
                source = self._coerce_chat_ref(entity_or_chat_id)
                target = self._coerce_chat_ref(target_chat_id)
                logging.getLogger("archive.lookup").info(
                    "[ArchiveLookup] source_chat_ref=%s message_id=%s exact_source=%s",
                    pseudonymize_engagement_subject(source),
                    message_id,
                    exact_source,
                )
                message = await self._get_archive_message_with_entity_fallback(
                    source, message_id, exact_source=exact_source
                )
                if isinstance(message, ArchiveMediaResult):
                    return message
                if not message or not getattr(message, "media", None):
                    detail = f"No archiveable media for chat={entity_or_chat_id} msg={message_id}"
                    logger.warning(f"    ⚠️ [UserAgent] {detail}")
                    return ArchiveMediaResult(ok=False, code="not_found", detail=detail)

                size_result = self._archive_size_result(message)
                if size_result is not None:
                    logger.warning(
                        f"    ⚠️ [UserAgent] Archive skipped for msg={message_id}: {size_result.detail}"
                    )
                    return size_result

                temp_path = self._archive_temp_path(message, message_id)
                try:
                    downloaded_path = await asyncio.wait_for(
                        self.client.download_media(message, file=temp_path),
                        timeout=settings.ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    return ArchiveMediaResult(
                        ok=False,
                        code="timeout",
                        detail=f"Download exceeded {settings.ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS}s.",
                        size_bytes=getattr(getattr(message, "file", None), "size", None),
                    )

                if downloaded_path:
                    temp_path = str(downloaded_path)

                result = await self._send_archived_file_with_retries(
                    target,
                    temp_path,
                    topic_id,
                    caption,
                )
                result.size_bytes = getattr(getattr(message, "file", None), "size", None)
                if result.ok:
                    logger.info(
                        f"    📦 [UserAgent] Archived media msg={message_id} from {entity_or_chat_id} to {target_chat_id}"
                    )
                else:
                    logger.warning(
                        f"    ⚠️ [UserAgent] Archive upload failed for msg={message_id}: {result.code} {result.detail}"
                    )
                return result
            except Exception as e:
                logger.warning(
                    f"    ⚠️ [UserAgent] Transient media archive failed for chat={entity_or_chat_id} msg={message_id}: {e}"
                )
                return ArchiveMediaResult(ok=False, code="error", detail=str(e)[:200])
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception as cleanup_err:
                        logger.warning(f"    ⚠️ [UserAgent] Temp archive cleanup failed: {cleanup_err}")
                await self._disconnect()

    async def clear_removed_users(self, group_id: int | str) -> int:
        async with self.lock:
            if not await self.start():
                return 0
            cleared_count = 0
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                from telethon.tl.functions.channels import EditBannedRequest
                from telethon.tl.types import ChannelParticipantsKicked, ChatBannedRights
                async for user in self.client.iter_participants(entity, filter=ChannelParticipantsKicked()):
                    try:
                        await self.client(EditBannedRequest(channel=entity, participant=user, banned_rights=ChatBannedRights(until_date=None, view_messages=False)))
                        cleared_count += 1
                    except Exception as _swallowed:
                        logger.debug(f"[suppressed] {_swallowed}")
                return cleared_count
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")
                return 0
            finally:
                await self._disconnect()

    async def cleanup_bots(
        self,
        group_id: int | str,
        whitelist_ids: list[int | str] | None = None,
        only_non_admins: bool = True,
    ) -> int:
        """Remove non-whitelisted bots, optionally preserving group admins."""
        async with self.lock:
            if not await self.start():
                return 0

            removed_count = 0
            whitelist = {
                str(identifier).strip().lstrip("@").casefold()
                for identifier in (whitelist_ids or [])
                if str(identifier).strip()
            }

            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                from telethon.tl.functions.channels import EditBannedRequest, GetParticipantRequest
                from telethon.tl.types import (
                    ChannelParticipantAdmin,
                    ChannelParticipantCreator,
                    ChatBannedRights,
                )

                logger.info(
                    "    🧹 [UserAgent] Starting bot cleanup in %s...",
                    getattr(entity, "title", group_id),
                )

                async for user in self.client.iter_participants(entity):
                    if not getattr(user, "bot", False) or getattr(user, "is_self", False):
                        continue

                    username = getattr(user, "username", None)
                    if str(user.id) in whitelist or (
                        username and username.casefold() in whitelist
                    ):
                        continue

                    if only_non_admins:
                        try:
                            result = await self.client(
                                GetParticipantRequest(channel=entity, participant=user)
                            )
                        except Exception as exc:
                            logger.warning(
                                "    ⚠️ [UserAgent] Cannot verify admin status for bot subject=%s; skipping cleanup: %s",
                                _subject_label(user.id),
                                type(exc).__name__,
                            )
                            continue

                        if isinstance(
                            result.participant,
                            (ChannelParticipantAdmin, ChannelParticipantCreator),
                        ):
                            continue

                    try:
                        await self.client(
                            EditBannedRequest(
                                channel=entity,
                                participant=user,
                                banned_rights=ChatBannedRights(
                                    until_date=None,
                                    view_messages=True,
                                ),
                            )
                        )
                        removed_count += 1
                        try:
                            await self.client(
                                EditBannedRequest(
                                    channel=entity,
                                    participant=user,
                                    banned_rights=ChatBannedRights(
                                        until_date=None,
                                        view_messages=False,
                                    ),
                                )
                            )
                        except Exception as exc:
                            logger.warning(
                                "    ⚠️ [UserAgent] Removed bot subject=%s but could not clear its ban: %s",
                                _subject_label(user.id),
                                type(exc).__name__,
                            )
                    except Exception as exc:
                        logger.error(
                            "    ❌ [UserAgent] Failed to remove bot subject=%s: %s",
                            _subject_label(user.id),
                            type(exc).__name__,
                        )

                logger.info(
                    "    ✨ [UserAgent] Bot cleanup complete. Removed %d bot(s).",
                    removed_count,
                )
                return removed_count
            except Exception as exc:
                logger.error(
                    "    ❌ [UserAgent] Bot cleanup failed: %s",
                    type(exc).__name__,
                )
                return removed_count
            finally:
                await self._disconnect()

    async def delete_old_messages(self, group_id: int | str, age_hours: int, topic_id: int | None = None) -> int:
        async with self.lock:
            if not await self.start():
                return 0
            import datetime

            from telethon.tl.types import Message
            deleted_count = 0
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                now = datetime.datetime.now(datetime.UTC)
                cutoff = now - datetime.timedelta(hours=age_hours)
                async for message in self.client.iter_messages(entity, reply_to=topic_id):
                    if not isinstance(message, Message):
                        continue
                    if message.date < cutoff:
                        try:
                            await self.client.delete_messages(entity, [message.id])
                            deleted_count += 1
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                return deleted_count
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")
                return 0
            finally:
                await self._disconnect()

    async def get_last_message_id(self, group_id: int | str, topic_id: int) -> int | None:
        async with self.lock:
            if not await self.start():
                return None
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                messages = await self.client.get_messages(entity, limit=1, reply_to=topic_id)
                if messages:
                    return messages[0].id
                return None
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")
                return None
            finally:
                await self._disconnect()

    async def get_history(self, group_id: int | str, limit: int) -> list[dict]:
        import os as _os

        from telethon.errors import FloodWaitError
        from telethon.tl.types import Message
        # Minimum sleep between successive get_history calls on the same session.
        # Prevents back-to-back MTProto requests across concurrent Celery tasks from
        # triggering Telegram FloodWait. Tune via MTPROTO_INTER_REQUEST_SLEEP (default 3s).
        INTER_SLEEP = float(_os.getenv("MTPROTO_INTER_REQUEST_SLEEP", 3.0))  # noqa: N806 — function-scoped constant
        async with self.lock:
            if not await self.start():
                return []
            msgs = []
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                async for message in self.client.iter_messages(entity, limit=limit):
                    if not isinstance(message, Message):
                        continue
                    content = message.text or ""
                    media_type, file_meta = _telethon_media_info(message)
                    sender_name = "Unknown"
                    if message.sender:
                        if hasattr(message.sender, 'username') and message.sender.username:
                            sender_name = message.sender.username
                        elif hasattr(message.sender, 'first_name'):
                            sender_name = message.sender.first_name
                    msgs.append({
                        "telegram_msg_id": message.id, "sender_name": sender_name, "content": content,
                        "media_type": media_type, "file_meta": file_meta, "chat_id": entity.id if hasattr(entity, 'id') else group_id
                    })
            except FloodWaitError as fwe:
                # Surface FloodWait so caller and logs know -- swallowing it hides the signal
                logger.warning(f"    🛑 [UserAgent] FloodWait in get_history for {group_id}: {fwe.seconds}s")
                from app.core.redis_srv import redis_srv
                session_name = self.current_session_name or "unknown"
                cooldown_key = f"user_agent:cooldown:{session_name}"
                wait = fwe.seconds + 60  # buffer
                if wait > 3600:
                    logger.error(f"    🛑 [UserAgent] SEVERE FLOOD WAIT for '{session_name}': {wait}s.")
                redis_srv.set_cooldown(cooldown_key, wait)
            except Exception as e:
                logger.debug(f"    ⚠️ [UserAgent] get_history error for {group_id}: {e}")
            finally:
                # Inter-request sleep INSIDE the lock so concurrent workers naturally queue
                # behind each other with spacing instead of all firing at once.
                await asyncio.sleep(INTER_SLEEP)
                await self._disconnect()
            return msgs

    async def search_messages(
        self, query: str, limit: int = 100, *, raise_errors: bool = False
    ) -> list[dict]:
        """
        Telegram global search via MTProto SearchGlobalRequest.

        Searches public channels Telegram has indexed (different result space
        from any web scanner). Same lock + cooldown discipline as get_history:
        FloodWait -> redis cooldown on the session, no client kept hot.

        Returns: list of {"text", "chat_id", "chat_name", "message_id", "date"}.
        By default, search errors return an empty list for older callers. The
        Telegram search scanner can request errors be raised so it can keep
        failed searches for its next run.
        """
        import os as _os

        from telethon.errors import FloodWaitError
        from telethon.tl.functions.messages import SearchGlobalRequest
        from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

        INTER_SLEEP = float(_os.getenv("MTPROTO_INTER_REQUEST_SLEEP", 3.0))  # noqa: N806 — function-scoped constant
        results: list[dict] = []

        async with self.lock:
            if not await self.start():
                if raise_errors:
                    raise UserAgentSessionUnavailableError(
                        f"No usable Telegram session is available for search {query!r}; "
                        "sessions may be missing, logged out, on cooldown, or unable to connect."
                    )
                return []
            try:
                # SearchGlobalRequest needs an InputPeer for offset_peer; use empty.
                res = await self.client(SearchGlobalRequest(
                    q=query,
                    filter=InputMessagesFilterEmpty(),
                    min_date=None,
                    max_date=None,
                    offset_rate=0,
                    offset_peer=InputPeerEmpty(),
                    offset_id=0,
                    limit=limit,
                ))

                # Build chat_id -> chat_name map from res.chats
                chat_map = {}
                for chat in (getattr(res, "chats", []) or []):
                    cid = getattr(chat, "id", None)
                    if cid is None:
                        continue
                    chat_map[cid] = (
                        getattr(chat, "title", None)
                        or getattr(chat, "username", None)
                        or "unknown"
                    )

                for msg in (getattr(res, "messages", []) or []):
                    text = getattr(msg, "message", None)
                    if not text:
                        continue

                    chat_id = None
                    chat_name = None
                    pid = getattr(msg, "peer_id", None)
                    if pid is not None:
                        if hasattr(pid, "channel_id"):
                            raw_id = pid.channel_id
                            chat_id = -1000000000000 - raw_id  # supergroup convention
                            chat_name = chat_map.get(raw_id)
                        elif hasattr(pid, "chat_id"):
                            raw_id = pid.chat_id
                            chat_id = -raw_id
                            chat_name = chat_map.get(raw_id)
                        elif hasattr(pid, "user_id"):
                            chat_id = pid.user_id
                            chat_name = chat_map.get(pid.user_id)

                    results.append({
                        "text": text,
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "message_id": getattr(msg, "id", None),
                        "date": str(getattr(msg, "date", None)) if getattr(msg, "date", None) else None,
                    })

                logger.info(f"    🔎 [UserAgent] SearchGlobal('{query[:40]}') -> {len(results)} messages")

            except FloodWaitError as fwe:
                logger.warning(
                    f"    🛑 [UserAgent] FloodWait on search '{query}': "
                    f"{fwe.seconds}s -- marking session cooldown"
                )
                session_name = self.current_session_name or "unknown"
                wait = fwe.seconds + 60
                try:
                    from app.core.redis_srv import redis_srv
                    redis_srv.set_cooldown(f"user_agent:cooldown:{session_name}", wait)
                except Exception as cooldown_error:
                    logger.error(
                        f"    ❌ [UserAgent] Could not save the cooldown after search "
                        f"'{query}' FloodWait: {cooldown_error}"
                    )
                    if not raise_errors:
                        raise
                if raise_errors:
                    raise
            except Exception as e:
                logger.error(
                    f"    ❌ [UserAgent] search_messages for '{query}' failed: {e}"
                )
                if raise_errors:
                    raise
            finally:
                await asyncio.sleep(INTER_SLEEP)
                await self._disconnect()

        return results
