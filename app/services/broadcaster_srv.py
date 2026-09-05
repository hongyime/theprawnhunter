import asyncio
import itertools
import logging
import math
import mimetypes
import time
from typing import Any

from telegram import Bot
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.request import HTTPXRequest

from app.core.config import settings
from app.core.database import db
from app.core.security import security
from app.services.user_agent_srv import user_agent

logger = logging.getLogger("broadcaster")

ARCHIVE_MEDIA_TYPES = {"document", "photo", "audio", "video"}
ANDROID_PACKAGE_MIME = "application/vnd.android.package-archive"

for _android_package_ext in (".apk", ".apks", ".xapk"):
    mimetypes.add_type(ANDROID_PACKAGE_MIME, _android_package_ext, strict=False)


def _media_filename(file_meta: dict[str, Any], media_type: str) -> str | None:
    raw_name = file_meta.get("file_name") if isinstance(file_meta, dict) else None
    if isinstance(raw_name, str) and raw_name.strip():
        filename = raw_name.replace("\\", "/").split("/")[-1].strip()
        filename = filename.replace("\r", "_").replace("\n", "_")
        return filename[:180] or None

    return {
        "photo": "photo.jpg",
        "video": "video.mp4",
        "audio": "audio",
        "document": "document.bin",
    }.get(media_type)


class BroadcastSendError(RuntimeError):
    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        retryable: bool = True,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def _classify_broadcast_exception(exc: BaseException) -> BroadcastSendError:
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, RetryAfter):
        retry_after = getattr(exc, "retry_after", None)
        return BroadcastSendError(
            "flood_wait",
            text,
            retryable=True,
            retry_after_seconds=int(retry_after) if retry_after else None,
        )
    if isinstance(exc, TimedOut | TimeoutError | asyncio.TimeoutError):
        return BroadcastSendError("timeout", text or "Telegram send timed out.", retryable=True)
    if isinstance(exc, Forbidden):
        return BroadcastSendError("forbidden", text, retryable=False)
    if isinstance(exc, BadRequest):
        if (
            "message thread not found" in lower
            or "topic_deleted" in lower
            or "topic deleted" in lower
        ):
            return BroadcastSendError("topic_missing", text, retryable=True)
        return BroadcastSendError("bad_request", text, retryable=False)
    if isinstance(exc, NetworkError):
        return BroadcastSendError("network_disconnect", text, retryable=True)
    if isinstance(exc, TelegramError):
        return BroadcastSendError("telegram_error", text, retryable=True)
    if isinstance(exc, BroadcastSendError):
        return exc
    return BroadcastSendError("all_identities_failed", text or exc.__class__.__name__, retryable=True)


async def _async_execute(query_builder):
    """Run synchronous PostgREST calls without blocking the event loop."""
    return await asyncio.to_thread(query_builder.execute)


def _username_from_meta(meta: object) -> str | None:
    if not isinstance(meta, dict):
        return None
    username = meta.get("bot_username")
    if not isinstance(username, str):
        return None
    username = username.strip()
    if not username:
        return None
    return username if username.startswith("@") else f"@{username}"


class BroadcasterService:
    def __init__(self):
        self.bot_tokens = settings.bot_tokens
        self._bots = {} # token -> Bot instance
        self._failed_tokens: set = set()
        self._archive_tasks: set[asyncio.Task] = set()

        # Rotation pool: bots ONLY for broadcast messages.
        # MTProto user sessions are reserved exclusively for admin operations
        # (topic creation, group management, Matkap scraping).
        # Sending broadcasts via a real user account is an OPSEC risk — real
        # phone numbers are visible to group admins in the member list.
        self._pool = []
        for token in self.bot_tokens:
            self._pool.append({"type": "bot", "id": token})

        self._cycle = itertools.cycle(self._pool)
        self._last_send_time = 0
        self._last_local_log_send = 0.0
        self._last_log_failure_warning = 0.0
        from app.core.constants import BROADCAST_RATE_LIMIT_SLEEP
        self._min_delay = BROADCAST_RATE_LIMIT_SLEEP

    def _get_bot_instance(self, token: str) -> Bot:
        if token not in self._bots:
            request = HTTPXRequest(
                connection_pool_size=100,
                pool_timeout=60.0,
                read_timeout=25.0,
                write_timeout=25.0,
            )
            self._bots[token] = Bot(token=token, request=request)
        return self._bots[token]

    async def _wait_for_rate_limit(self):
        """Ensures a minimum delay between ANY two messages sent by the system."""
        elapsed = time.time() - self._last_send_time
        if elapsed < self._min_delay:
            wait_time = self._min_delay - elapsed
            await asyncio.sleep(wait_time)
        self._last_send_time = time.time()

    async def _resolve_chat_id(self, msg_obj: dict) -> int | str | None:
        cred_info = msg_obj.get("discovered_credentials") or msg_obj.get("credential") or {}
        if isinstance(cred_info, dict):
            username = _username_from_meta(cred_info.get("meta"))
            if username:
                return username
            joined_chat_id = cred_info.get("chat_id")
            if joined_chat_id:
                return joined_chat_id

        direct_chat_id = msg_obj.get("chat_id")
        if direct_chat_id:
            return direct_chat_id

        cred_id = msg_obj.get("credential_id")
        if not cred_id:
            return None

        try:
            from app.core.database import db

            res = await _async_execute(
                db.table("discovered_credentials")
                .select("chat_id, meta")
                .eq("id", cred_id)
                .limit(1)
            )
            rows = res.data or []
            if not rows:
                return None
            row = rows[0]
            username = _username_from_meta(row.get("meta"))
            if username:
                return username
            return row.get("chat_id")
        except Exception as exc:
            logger.warning(
                f"[Broadcaster] Failed to resolve source chat_id for credential={cred_id}: {exc}"
            )
            return None

    async def _auto_archive_media(self, group_id: int | str, thread_id: int, msg_obj: dict, msg_id):
        source_chat_id = await self._resolve_chat_id(msg_obj)
        if not source_chat_id:
            logger.warning(f"[Broadcaster] Auto-archive skipped for msg={msg_id}: missing source chat_id")
            return None
        result = await user_agent.archive_media_transiently(
            source_chat_id,
            int(msg_obj["telegram_msg_id"]),
            target_chat_id=group_id,
            topic_id=thread_id,
            caption=f"Archived Attachment [ID: {msg_obj.get('id', msg_id)}]",
        )
        if not result.ok:
            logger.warning(
                f"[Broadcaster] Auto-archive skipped for msg={msg_id}: {result.code} {result.detail}"
            )
        return result

    def _schedule_auto_archive(self, group_id: int | str, thread_id: int, msg_obj: dict, msg_id):
        task = asyncio.create_task(self._auto_archive_media(group_id, thread_id, msg_obj, msg_id))
        self._archive_tasks.add(task)

        def _log_archive_result(done_task: asyncio.Task):
            self._archive_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                logger.debug(f"[Broadcaster] Auto-archive task cancelled for msg={msg_id}")
            except Exception:
                logger.exception(f"[Broadcaster] Auto-archive task crashed for msg={msg_id}")

        task.add_done_callback(_log_archive_result)

    async def _download_media_bytes(self, file_meta: dict, credential_id: str) -> bytes | None:
        """Download media bytes using the SOURCE bot's token (not the broadcaster's)."""
        if not credential_id or not file_meta:
            return None
        try:
            res = await _async_execute(
                db.table("discovered_credentials")
                .select("bot_token")
                .eq("id", credential_id)
                .limit(1)
            )
            rows = res.data or []
            if not rows or not rows[0].get("bot_token"):
                return None

            decrypted_token = security.decrypt(rows[0]["bot_token"]).strip()

            file_id = file_meta.get("file_id")
            if file_id:
                request = HTTPXRequest(read_timeout=15.0, write_timeout=15.0)
                source_bot = Bot(token=decrypted_token, request=request)
                tg_file = await source_bot.get_file(file_id)
                data = await tg_file.download_as_bytearray()
                logger.info(f"    📥 [Broadcaster] Downloaded {len(data)} bytes via Bot API source bot")
                return bytes(data)

            # Fallback to Telethon download if access_hash & id present
            media_id = file_meta.get("id")
            access_hash = file_meta.get("access_hash")
            if media_id and access_hash:
                from telethon.tl.types import InputDocument, InputPhoto

                from app.services.bot_manager_srv import bot_manager
                client = await bot_manager.get_client(decrypted_token)
                file_ref = bytes.fromhex(file_meta.get("file_reference", "")) if file_meta.get("file_reference") else b""

                wc = file_meta.get("wc")
                if wc == "photo":
                    input_location = InputPhoto(id=media_id, access_hash=access_hash, file_reference=file_ref)
                else:
                    input_location = InputDocument(id=media_id, access_hash=access_hash, file_reference=file_ref)

                data = await client.download_media(input_location, bytes)
                if data:
                    logger.info(f"    📥 [Broadcaster] Downloaded {len(data)} bytes via Telethon client")
                    return data
            return None
        except Exception as exc:
            logger.warning(f"    ⚠️ [Broadcaster] _download_media_bytes failed: {exc}")
            return None

    async def send_message(self, group_id: int | str, thread_id: int, msg_obj: dict):
        """
        Sends a message using the next available identity (Bot or User Account).
        """
        content = msg_obj.get("content", "")
        sender = msg_obj.get("sender_name", "Unknown")
        media_type = msg_obj.get("media_type", "text")
        msg_id = msg_obj.get("telegram_msg_id", "?")

        caption = f"[ID: {msg_id}] [From: {sender}]\n{content}"
        if len(caption) > 1024:
            caption = caption[:1021] + "..."

        to_send_text = caption
        if media_type == "photo":
            to_send_text = f"{caption}\n\n[Photo Media Detected]"
        elif media_type != "text":
            to_send_text = f"{caption}\n\n[{media_type} Media Detected]"

        # Try up to N times (total size of pool)
        last_failure: BroadcastSendError | None = None
        for _ in range(len(self._pool)):
            identity = next(self._cycle)

            await self._wait_for_rate_limit()

            if identity["type"] == "bot":
                token = identity["id"]
                if token in self._failed_tokens: continue

                bot = self._get_bot_instance(token)
                try:
                    logger.info(f"📤 [Broadcaster] Sending via Bot: {token[:10]}...")

                    file_meta = msg_obj.get("file_meta") or {}
                    file_id = file_meta.get("file_id")
                    has_telethon_hash = bool(file_meta.get("id") and file_meta.get("access_hash"))
                    bot_thread_id = thread_id if thread_id != 1 else None
                    sent_via_media = False
                    media_delivery_failure: BroadcastSendError | None = None

                    logger.info(f"    🔍 DEBUG: media_type={media_type}, file_id={file_id}, fm={file_meta}")

                    if (file_id or has_telethon_hash) and media_type in ARCHIVE_MEDIA_TYPES:
                        media_bytes = await self._download_media_bytes(file_meta, msg_obj.get("credential_id"))
                        if media_bytes:
                            filename = _media_filename(file_meta, media_type)
                            try:
                                if media_type == "photo":
                                    await bot.send_photo(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        photo=media_bytes,
                                        caption=caption,
                                        filename=filename,
                                    )
                                elif media_type == "document":
                                    await bot.send_document(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        document=media_bytes,
                                        caption=caption,
                                        filename=filename,
                                        disable_content_type_detection=False,
                                    )
                                elif media_type == "video":
                                    await bot.send_video(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        video=media_bytes,
                                        caption=caption,
                                        filename=filename,
                                    )
                                elif media_type == "audio":
                                    await bot.send_audio(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        audio=media_bytes,
                                        caption=caption,
                                        filename=filename,
                                    )

                                sent_via_media = True
                                logger.info(
                                    f"    ✅ [Broadcaster] Successfully sent {media_type} "
                                    f"({len(media_bytes)} bytes, filename={filename})"
                                )
                            except TelegramError as media_err:
                                media_delivery_failure = _classify_broadcast_exception(media_err)
                                last_failure = media_delivery_failure
                                logger.warning(f"⚠️ Failed to send media bytes: {media_err}.")

                    if not sent_via_media and media_type in ARCHIVE_MEDIA_TYPES:
                        if settings.AUTO_ARCHIVE_MEDIA and msg_obj.get("telegram_msg_id"):
                            archive_result = await self._auto_archive_media(
                                group_id,
                                thread_id,
                                msg_obj,
                                msg_id,
                            )
                            if archive_result and archive_result.ok:
                                return

                            detail = "Media could not be archived."
                            code = "media_archive_failed"
                            retryable = True
                            if archive_result is not None:
                                detail = archive_result.detail or archive_result.code
                                code = f"media_archive_{archive_result.code}"
                                retryable = archive_result.code not in {"too_large", "not_found", "missing_access_hash"}
                            last_failure = BroadcastSendError(code, detail, retryable=retryable)
                        elif media_delivery_failure is None:
                            last_failure = BroadcastSendError(
                                "media_unavailable",
                                "Media bytes were unavailable and AUTO_ARCHIVE_MEDIA is disabled.",
                                retryable=True,
                            )

                        raise last_failure

                    if not sent_via_media:
                        await bot.send_message(
                            chat_id=group_id,
                            message_thread_id=bot_thread_id,
                            text=to_send_text
                        )
                    return
                except Forbidden as e:
                    self._failed_tokens.add(token)
                    last_failure = _classify_broadcast_exception(e)
                    logger.warning(f"⚠️ Bot {token[:10]}... kicked. Rotating...")
                except RetryAfter as e:
                    last_failure = _classify_broadcast_exception(e)
                    logger.warning(f"⚠️ Bot {token[:10]}... flood-waited. Rotating...")
                except (TimedOut, NetworkError, asyncio.TimeoutError, TimeoutError) as e:
                    last_failure = _classify_broadcast_exception(e)
                    logger.warning(f"⚠️ Bot send transient failure: {last_failure.reason}: {e}")
                except TelegramError as e:
                    last_failure = _classify_broadcast_exception(e)
                    if last_failure.reason == "topic_missing":
                        # Let the caller (broadcast_logic) handle topic recreation
                        # instead of silently dumping into General
                        raise last_failure from e
                    logger.error(f"❌ Bot send failed: {e}")
                    if not last_failure.retryable:
                        raise last_failure from e

        logger.error("❌ All identities failed to send message.")
        if last_failure:
            raise last_failure
        raise BroadcastSendError(
            "all_identities_failed",
            "All identities failed to send message",
            retryable=True,
        )

    def _acquire_system_log_slot(self) -> bool:
        min_interval = float(getattr(settings, "TELEGRAM_LOG_MIN_INTERVAL_SECONDS", 2.0) or 0)
        if min_interval <= 0:
            return True

        try:
            import redis

            client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            ttl = max(1, int(math.ceil(min_interval)))
            return bool(client.set("telegram:system_log:send_cooldown", "1", nx=True, ex=ttl))
        except Exception:
            now = time.monotonic()
            if now - self._last_local_log_send < min_interval:
                return False
            self._last_local_log_send = now
            return True

    def _warn_system_log_failure(self, exc: BaseException) -> None:
        warn_interval = int(getattr(settings, "TELEGRAM_LOG_FAILURE_WARN_INTERVAL_SECONDS", 60) or 0)
        if warn_interval <= 0:
            logger.warning(f"Failed to send log: {exc}")
            return

        try:
            import redis

            client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            if client.set("telegram:system_log:failure_warning_cooldown", "1", nx=True, ex=warn_interval):
                logger.warning(f"Failed to send log: {exc}")
            else:
                logger.debug(f"Suppressed repeated Telegram log send failure: {exc}")
            return
        except Exception:
            now = time.monotonic()
            if now - self._last_log_failure_warning >= warn_interval:
                self._last_log_failure_warning = now
                logger.warning(f"Failed to send log: {exc}")
            else:
                logger.debug(f"Suppressed repeated Telegram log send failure: {exc}")

    async def send_log(self, message: str):
        """Sends a log to the General topic using a healthy bot."""
        if not self._acquire_system_log_slot():
            logger.debug("Suppressed Telegram system log due to rate limit")
            return False

        await self._wait_for_rate_limit()
        try:
            bot = self._get_bot_instance(self.bot_tokens[0])
            await bot.send_message(
                chat_id=settings.MONITOR_GROUP_ID,
                text=f"🤖 [System Log]\n{message}"
            )
            return True
        except Exception as e:
            self._warn_system_log_failure(e)
            return False

    async def send_to_thread(
        self,
        group_id: int | str,
        thread_id: int,
        text: str,
        parse_mode: str | None = "Markdown",
    ) -> int | None:
        """Send a raw text message to a specific forum topic thread.
        Returns the sent Telegram message_id, or None on failure.
        """
        await self._wait_for_rate_limit()
        bot = self._get_bot_instance(self.bot_tokens[0])
        try:
            bot_thread_id = thread_id if thread_id and thread_id != 1 else None
            sent = await bot.send_message(
                chat_id=group_id,
                message_thread_id=bot_thread_id,
                text=text,
                parse_mode=parse_mode,
            )
            return getattr(sent, "message_id", None)
        except Exception as e:
            logger.warning(f"[Broadcaster] send_to_thread failed thread={thread_id}: {e}")
            return None

    async def pin_message(
        self,
        group_id: int | str,
        message_id: int,
        disable_notification: bool = True,
    ) -> bool:
        """Pin a Telegram message in the group. Returns True on success."""
        await self._wait_for_rate_limit()
        bot = self._get_bot_instance(self.bot_tokens[0])
        try:
            await bot.pin_chat_message(
                chat_id=group_id,
                message_id=message_id,
                disable_notification=disable_notification,
            )
            return True
        except Exception as e:
            logger.warning(f"[Broadcaster] pin_message failed msg_id={message_id}: {e}")
            return False

    async def unpin_message(
        self,
        group_id: int | str,
        message_id: int,
    ) -> bool:
        """Unpin a specific Telegram message in the group. Returns True on success."""
        await self._wait_for_rate_limit()
        bot = self._get_bot_instance(self.bot_tokens[0])
        try:
            await bot.unpin_chat_message(
                chat_id=group_id,
                message_id=message_id,
            )
            return True
        except Exception as e:
            logger.warning(f"[Broadcaster] unpin_message failed msg_id={message_id}: {e}")
            return False

    async def ensure_topic(self, group_id: int | str, topic_name: str) -> int:
        """Ensures a forum topic exists. Retries once before raising."""
        try:
            existing_id = await user_agent.find_topic_id(group_id, topic_name)
            if existing_id: return existing_id
        except Exception: pass

        if topic_name in ["General", "general", "main"]: return 1

        bot = self._get_bot_instance(self.bot_tokens[0])
        last_err = None
        for attempt in range(2):
            try:
                topic = await bot.create_forum_topic(chat_id=group_id, name=topic_name)
                return topic.message_thread_id
            except Exception as e:
                last_err = e
                if attempt == 0:
                    logger.warning(f"Topic creation attempt 1 failed: {e}. Retrying...")
                    await asyncio.sleep(2)

        logger.error(f"Topic creation failed after 2 attempts: {last_err}")
        raise RuntimeError(f"Could not create topic '{topic_name}': {last_err}")

    async def rename_topic(self, group_id: int | str, thread_id: int, new_name: str) -> bool:
        """Renames an existing forum topic."""
        bot = self._get_bot_instance(self.bot_tokens[0])
        try:
            await bot.edit_forum_topic(
                chat_id=group_id,
                message_thread_id=thread_id,
                name=new_name,
            )
            logger.info(f"Renamed topic {thread_id} to '{new_name}'")
            return True
        except Exception as e:
            logger.warning(f"Topic rename failed for {thread_id}: {e}")
            return False

    async def close_topic(self, group_id: int | str, thread_id: int) -> bool:
        """Closes an existing forum topic without deleting its history."""
        try:
            thread_id_int = int(thread_id)
        except (TypeError, ValueError):
            logger.warning(f"Topic close skipped for invalid thread_id={thread_id}")
            return False

        if thread_id_int <= 1:
            logger.warning(f"Topic close skipped for invalid thread_id={thread_id}")
            return False

        bot = self._get_bot_instance(self.bot_tokens[0])
        try:
            await bot.close_forum_topic(
                chat_id=group_id,
                message_thread_id=thread_id_int,
            )
            logger.info(f"Closed topic {thread_id_int}")
            return True
        except Exception as e:
            err = str(e).lower()
            if (
                "topic_closed" in err
                or "topic is closed" in err
                or "topic_not_modified" in err
                or "not modified" in err
            ):
                logger.info(f"Topic {thread_id_int} already closed")
                return True
            logger.warning(f"Topic close failed for {thread_id}: {e}")
            return False
