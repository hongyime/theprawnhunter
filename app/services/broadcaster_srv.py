import asyncio
import itertools
import logging
import math
import mimetypes
import time
from typing import Any

from telegram import Bot, LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.request import HTTPXRequest

from app.core.config import settings
from app.core.database import db
from app.core.security import security
from app.infrastructure.database import execute_async as _execute_async_shared
from app.services.archive_source import (
    archive_message_id,
    honeypot_source_chat_id,
    is_honeypot_message,
)
from app.services.honeypot_urls import format_link_caption, format_link_preview
from app.services.message_preview import TEXT_PREVIEW_VERSION, TextPreview, utf16_length
from app.services.note_transfer import (
    NOTE_MEDIA_TYPES,
    NoteMediaTooLargeError,
    NoteTransfer,
    NoteTransferResult,
    is_note_media_type,
    relay_note_media,
)
from app.services.user_agent_srv import UserAgentSessionUnavailableError, user_agent

logger = logging.getLogger("broadcaster")

ARCHIVE_MEDIA_TYPES = {"document", "photo", "audio", "video", "animation"} | NOTE_MEDIA_TYPES
ANDROID_PACKAGE_MIME = "application/vnd.android.package-archive"

for _android_package_ext in (".apk", ".apks", ".xapk"):
    mimetypes.add_type(ANDROID_PACKAGE_MIME, _android_package_ext, strict=False)

# IS-2: General carries only high-signal alerts; routine system chatter goes to
# a dedicated topic. "General" is topic id 1, but the Telegram Bot API expects
# `message_thread_id` to be omitted for General (see send_to_thread), so the
# payload builder maps id 1 -> None.
SYSTEM_LOGS_TOPIC_NAME = "System Logs"
SYSTEM_LOGS_TOPIC_CACHE_TTL = 86400
GENERAL_THREAD_ID = 1


def _system_logs_topic_cache_key(group_id: int | str) -> str:
    return f"topic:system_logs:{group_id}"


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
        "animation": "animation.mp4",
    }.get(media_type)


def _cap_utf16(text: str, limit: int) -> str:
    """Truncate to `limit` UTF-16 units with an ellipsis (display-only cap)."""
    if utf16_length(text) <= limit:
        return text
    enc = text.encode("utf-16-le")
    return enc[: max(0, limit - 3) * 2].decode("utf-16-le", errors="ignore") + "..."


def _cut_at_units(text: str, budget: int) -> int:
    """Return the char index that fits within `budget` UTF-16 code units."""
    units = 0
    cut = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > budget:
            break
        cut = i + 1
    return cut or 1


def _split_utf16_text(text: str, limit: int = 4096) -> list[str]:
    """Split text into Telegram-safe chunks measured in UTF-16 units.

    Chunks are numbered "(n/total)" only when there is more than one. The
    split budget leaves headroom for the numbering suffix so the final
    message never exceeds Telegram's 4096-character limit.
    """
    if utf16_length(text) <= limit:
        return [text]

    budget = max(1, limit - 32)
    words = text.split(" ")
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if utf16_length(candidate) > budget:
            if current:
                chunks.append(current)
                current = word
            else:
                while utf16_length(word) > budget:
                    cut = _cut_at_units(word, budget)
                    chunks.append(word[:cut])
                    word = word[cut:]
                current = word
        else:
            current = candidate
    if current:
        chunks.append(current)

    total = len(chunks)
    if total <= 1:
        return chunks
    return [f"{chunk} ({i}/{total})" for i, chunk in enumerate(chunks, 1)]


def _source_label(file_meta: dict[str, Any]) -> str:
    chat_type = file_meta.get("source_chat_type")
    title = file_meta.get("source_chat_title")
    if chat_type == "private":
        return "[DM]"
    if chat_type in ("group", "supergroup"):
        return f"[Group: {title}]" if isinstance(title, str) and title.strip() else "[Group]"
    if chat_type == "channel":
        return f"[Channel: {title}]" if isinstance(title, str) and title.strip() else "[Channel]"
    return ""


async def _mute_topic_best_effort(group_id: int | str, topic_id: int) -> bool:
    if not getattr(settings, "AUTO_MUTE_TOPICS", True):
        return False
    try:
        return await user_agent.mute_topic(group_id, topic_id)
    except Exception as exc:
        logger.warning(f"[Broadcaster] Could not mute topic {topic_id}: {exc}")
        return False


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
    return await _execute_async_shared(query_builder)


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
        # PERF-002: monotonic counter incremented every time RetryAfter
        # (a.k.a. flood_wait) is caught during a send. Read by
        # flow_tasks._broadcast_logic to adapt inter-message delay without
        # inspecting exceptions itself. Counter is monotonic; callers detect
        # events via delta.
        self._flood_wait_hits: int = 0

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
        if is_honeypot_message(msg_obj):
            return honeypot_source_chat_id(msg_obj)
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
        source_message_id = archive_message_id(msg_obj)
        if source_message_id is None:
            logger.warning("[Broadcaster] Auto-archive skipped: original source message ID unavailable")
            return None
        source_chat_id = await self._resolve_chat_id(msg_obj)
        if not source_chat_id:
            logger.warning(f"[Broadcaster] Auto-archive skipped for msg={msg_id}: missing source chat_id")
            return None
        result = await user_agent.archive_media_transiently(
            source_chat_id,
            source_message_id,
            target_chat_id=group_id,
            topic_id=thread_id,
            caption=f"Archived Attachment [ID: {msg_obj.get('id', msg_id)}]",
            exact_source=is_honeypot_message(msg_obj),
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
                # PERF-003: reuse pooled Bot instance keyed by token via
                # _get_bot_instance instead of allocating a fresh HTTPXRequest
                # + Bot per media download. Halves the setup overhead on
                # repeated downloads from the same source bot.
                source_bot = self._get_bot_instance(decrypted_token)
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
                # PERF-003: bot_manager pools TelegramClient instances keyed
                # by token — this get_client() call hits the cache after the
                # first login for a given token (see BotClientManager
                # docstring). No fresh MTProto handshake per download.
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

    async def _persist_thumb_path(self, message_id: Any, file_meta: dict) -> None:
        """Merge ``thumb_path`` into the stored file_meta (best-effort).

        Re-reads the row first so a concurrent heavy-enrichment write (OCR
        text) is not clobbered by the thumbnail merge.
        """
        merged = dict(file_meta)
        res = await _async_execute(
            db.table("exfiltrated_messages").select("file_meta").eq("id", message_id).limit(1)
        )
        rows = res.data or []
        stored = rows[0].get("file_meta") if rows else None
        if isinstance(stored, dict):
            merged = {**stored, **merged}
        await _async_execute(
            db.table("exfiltrated_messages").update({"file_meta": merged}).eq("id", message_id)
        )

    async def _attach_thumbnail(self, msg_obj: dict, media_type: str, media_bytes: bytes | None) -> None:
        """Best-effort evidence thumbnail; a failure never changes the send outcome."""
        try:
            from app.services.thumbnails import attach_thumbnail

            await attach_thumbnail(
                msg_obj,
                media_type,
                media_bytes,
                persist=self._persist_thumb_path,
            )
        except Exception as exc:
            logger.warning(f"    ⚠️ [Broadcaster] thumbnail hook skipped: {exc}")

    async def _transfer_note_media(self, target: Bot, transfer: NoteTransfer) -> NoteTransferResult | None:
        if not transfer.credential_id:
            return None
        res = await _async_execute(
            db.table("discovered_credentials")
            .select("bot_token")
            .eq("id", transfer.credential_id)
            .limit(1)
        )
        rows = res.data or []
        if not rows or not rows[0].get("bot_token"):
            return None
        source_token = security.decrypt(rows[0]["bot_token"]).strip()
        return await relay_note_media(self._get_bot_instance(source_token), target, transfer)

    async def send_message(
        self,
        group_id: int | str,
        thread_id: int,
        msg_obj: dict,
    ) -> int | None:
        """Send a broadcast message.

        Returns the Telegram message_id on successful delivery (used by the
        caller to persist `exfiltrated_messages.broadcast_message_id` for
        idempotency on retry — see INTR-001). Returns ``None`` when the
        message was delivered via auto-archive or another path that doesn't
        expose a message-id. Raises ``BroadcastSendError`` on failure.
        """
        content = msg_obj.get("content", "")
        sender = msg_obj.get("sender_name", "Unknown")
        media_type = msg_obj.get("media_type", "text")
        msg_id = msg_obj.get("telegram_msg_id", "?")
        file_meta = msg_obj.get("file_meta") or {}
        urls = file_meta.get("urls") or []

        if media_type == "text" and not str(content or "").strip():
            raise BroadcastSendError(
                "empty_content",
                "Message has no text, caption, or media",
                retryable=False,
            )

        header = f"[ID: {msg_id}] [From: {sender}]"
        source_label = _source_label(file_meta)
        if source_label:
            header = f"{header} {source_label}"

        if file_meta.get("text_preview_version") == TEXT_PREVIEW_VERSION:
            full_text = format_link_preview(
                header, TextPreview(content, file_meta.get("content_truncated") is True), urls
            )
        else:
            full_text = format_link_caption(header, content, urls) if urls else f"{header}\n{content}"

        if media_type == "paid_media":
            star_count = file_meta.get("paid_media_star_count")
            if isinstance(star_count, int):
                full_text += f"\n\n[Paid media shared: {star_count} Stars]"
            else:
                full_text += "\n\n[Paid media shared]"
        elif media_type == "story":
            full_text += "\n\n[Story shared]"

        text_parts = _split_utf16_text(full_text)
        media_caption = header if utf16_length(header) <= 1024 else None
        to_send_text = _cap_utf16(full_text, 1024)
        follow_text_parts: list[str] | None = None
        if media_type in ARCHIVE_MEDIA_TYPES and str(content or "").strip():
            follow_text = (
                format_link_caption("", content, urls).strip("\n")
                if urls
                else str(content)
            )
            follow_text_parts = _split_utf16_text(follow_text)

        # Try up to N times (total size of pool)
        last_failure: BroadcastSendError | None = None
        # Telegram message-id of a successful send, or None when the delivery
        # path doesn't expose one (auto-archive fallback). Callers persist
        # this into exfiltrated_messages.broadcast_message_id for INTR-001.
        sent_message_id: int | None = None
        for _ in range(len(self._pool)):
            identity = next(self._cycle)

            await self._wait_for_rate_limit()

            if identity["type"] == "bot":
                token = identity["id"]
                if token in self._failed_tokens:
                    continue

                bot = self._get_bot_instance(token)
                try:
                    logger.info(f"📤 [Broadcaster] Sending via Bot: {token[:10]}...")

                    file_meta = msg_obj.get("file_meta") or {}
                    file_id = file_meta.get("file_id")
                    has_telethon_hash = bool(file_meta.get("id") and file_meta.get("access_hash"))
                    bot_thread_id = thread_id
                    sent_via_media = False
                    media_delivery_failure: BroadcastSendError | None = None

                    media_debug_meta = {
                        key: value for key, value in file_meta.items()
                        if key not in {"urls", "text_links"}
                    }
                    logger.info(f"    🔍 DEBUG: media_type={media_type}, file_id={file_id}, fm={media_debug_meta}")

                    if file_id and is_note_media_type(media_type):
                        try:
                            note_result = await self._transfer_note_media(bot, NoteTransfer(
                                kind=media_type,
                                credential_id=msg_obj.get("credential_id"),
                                file_id=file_id,
                                chat_id=group_id,
                                thread_id=thread_id,
                                summary=to_send_text,
                                has_urls=bool(urls),
                            ))
                            if note_result is not None:
                                sent_via_media = True
                                sent_message_id = note_result.summary.message_id
                                logger.info("[Broadcaster] Transferred %s file and summary", media_type)
                        except NoteMediaTooLargeError as size_err:
                            raise BroadcastSendError(
                                "media_archive_too_large", str(size_err), retryable=False
                            ) from size_err
                        except TelegramError as media_err:
                            media_delivery_failure = _classify_broadcast_exception(media_err)
                            if media_delivery_failure.reason == "flood_wait":
                                self._flood_wait_hits += 1
                            last_failure = media_delivery_failure
                            logger.warning("[Broadcaster] Note transfer failed: %s", media_delivery_failure.reason)

                    if (
                        (file_id or has_telethon_hash) and media_type in ARCHIVE_MEDIA_TYPES
                        and media_type not in NOTE_MEDIA_TYPES
                    ):
                        media_bytes = await self._download_media_bytes(file_meta, msg_obj.get("credential_id"))
                        if media_bytes:
                            filename = _media_filename(file_meta, media_type)
                            try:
                                if media_type == "photo":
                                    _sent = await bot.send_photo(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        photo=media_bytes,
                                        caption=media_caption,
                                        filename=filename,
                                    )
                                elif media_type == "document":
                                    _sent = await bot.send_document(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        document=media_bytes,
                                        caption=media_caption,
                                        filename=filename,
                                        disable_content_type_detection=False,
                                    )
                                elif media_type == "video":
                                    _sent = await bot.send_video(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        video=media_bytes,
                                        caption=media_caption,
                                        filename=filename,
                                    )
                                elif media_type == "audio":
                                    _sent = await bot.send_audio(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        audio=media_bytes,
                                        caption=media_caption,
                                        filename=filename,
                                    )
                                elif media_type == "animation":
                                    _sent = await bot.send_animation(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        animation=media_bytes,
                                        caption=media_caption,
                                        filename=filename,
                                    )
                                else:
                                    _sent = None

                                sent_via_media = True
                                sent_message_id = getattr(_sent, "message_id", None)
                                logger.info(
                                    f"    ✅ [Broadcaster] Successfully sent {media_type} "
                                    f"({len(media_bytes)} bytes, filename={filename})"
                                )
                                # Evidence thumbnail: image media only, and a
                                # failure must never affect the delivery above.
                                await self._attach_thumbnail(msg_obj, media_type, media_bytes)
                                if follow_text_parts:
                                    try:
                                        for _part in follow_text_parts:
                                            await bot.send_message(
                                                chat_id=group_id,
                                                message_thread_id=bot_thread_id,
                                                text=_part,
                                                link_preview_options=LinkPreviewOptions(is_disabled=True) if urls else None,
                                            )
                                    except TelegramError as _follow_err:
                                        logger.warning(
                                            f"⚠️ Follow-up text after {media_type} failed: {_follow_err}"
                                        )
                            except TelegramError as media_err:
                                media_delivery_failure = _classify_broadcast_exception(media_err)
                                if media_delivery_failure.reason == "flood_wait":
                                    self._flood_wait_hits += 1
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
                                if (
                                    media_type in NOTE_MEDIA_TYPES
                                    or file_meta.get("text_preview_version") == TEXT_PREVIEW_VERSION
                                ):
                                    summary = await bot.send_message(
                                        chat_id=group_id,
                                        message_thread_id=bot_thread_id,
                                        text=to_send_text,
                                        link_preview_options=LinkPreviewOptions(is_disabled=True) if urls else None,
                                    )
                                    return summary.message_id
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
                        first_id = None
                        for _part in text_parts:
                            _sent = await bot.send_message(
                                chat_id=group_id,
                                message_thread_id=bot_thread_id,
                                text=_part,
                                link_preview_options=LinkPreviewOptions(is_disabled=True) if urls else None,
                            )
                            if first_id is None:
                                first_id = getattr(_sent, "message_id", None)
                        sent_message_id = first_id
                    return sent_message_id
                except Forbidden as e:
                    self._failed_tokens.add(token)
                    last_failure = _classify_broadcast_exception(e)
                    logger.warning(f"⚠️ Bot {token[:10]}... kicked. Rotating...")
                except RetryAfter as e:
                    self._flood_wait_hits += 1
                    last_failure = _classify_broadcast_exception(e)
                    logger.warning(f"⚠️ Bot {token[:10]}... flood-waited. Rotating...")
                except (TimedOut, NetworkError, TimeoutError) as e:
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

    async def send_document(
        self,
        group_id: int | str,
        thread_id: int,
        document: bytes,
        filename: str,
    ) -> int | None:
        """Send a document to a topic, rotating through the broadcaster bots."""
        last_failure: BroadcastSendError | None = None
        for _ in range(len(self._pool)):
            identity = next(self._cycle)
            await self._wait_for_rate_limit()
            if identity["type"] != "bot":
                continue

            token = identity["id"]
            if token in self._failed_tokens:
                continue

            bot = self._get_bot_instance(token)
            try:
                sent = await bot.send_document(
                    chat_id=group_id,
                    message_thread_id=thread_id,
                    document=document,
                    filename=filename,
                    disable_content_type_detection=False,
                )
                return getattr(sent, "message_id", None)
            except Forbidden as exc:
                self._failed_tokens.add(token)
                last_failure = _classify_broadcast_exception(exc)
                logger.warning(f"⚠️ Bot {token[:10]}... kicked while sending document. Rotating...")
            except RetryAfter as exc:
                self._flood_wait_hits += 1
                last_failure = _classify_broadcast_exception(exc)
                logger.warning(f"⚠️ Bot {token[:10]}... flood-waited while sending document. Rotating...")
            except (TimedOut, NetworkError, TimeoutError) as exc:
                last_failure = _classify_broadcast_exception(exc)
                logger.warning(f"⚠️ Bot document send transient failure: {last_failure.reason}: {exc}")
            except TelegramError as exc:
                last_failure = _classify_broadcast_exception(exc)
                if last_failure.reason == "topic_missing" or not last_failure.retryable:
                    raise last_failure from exc
                logger.error(f"❌ Bot document send failed: {exc}")

        if last_failure:
            raise last_failure
        raise BroadcastSendError(
            "all_identities_failed",
            "All identities failed to send document",
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

    async def _send_control_message(
        self,
        text: str,
        *,
        message_thread_id: int | None = None,
    ) -> bool:
        """Best-effort control-channel delivery; rotates identities, never raises."""
        if not self._pool:
            return False

        last_exc: BaseException | None = None
        for _ in range(len(self._pool)):
            identity = next(self._cycle)
            if identity.get("type") != "bot":
                continue

            token = identity["id"]
            if token in self._failed_tokens:
                continue

            try:
                await self._wait_for_rate_limit()
                bot = self._get_bot_instance(token)
                payload: dict[str, Any] = {
                    "chat_id": settings.MONITOR_GROUP_ID,
                    "text": text,
                }
                if message_thread_id and message_thread_id != GENERAL_THREAD_ID:
                    payload["message_thread_id"] = message_thread_id
                await bot.send_message(**payload)
                return True
            except Forbidden as exc:
                self._failed_tokens.add(token)
                last_exc = exc
                logger.warning(f"⚠️ Control message bot {token[:10]}... kicked. Rotating...")
            except Exception as exc:
                last_exc = exc
                logger.warning(f"⚠️ Control message bot {token[:10]}... failed: {exc}")

        if last_exc is not None:
            self._warn_system_log_failure(last_exc)
        return False

    async def _resolve_system_logs_topic_id(self, group_id: int | str) -> int:
        cache_key = _system_logs_topic_cache_key(group_id)
        client = None
        try:
            import redis

            client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            cached = client.get(cache_key)
            if cached is not None:
                return int(cached)
        except Exception as exc:
            logger.debug(f"[Broadcaster] System Logs topic cache read failed: {exc}")

        topic_id = await self.ensure_topic(group_id, SYSTEM_LOGS_TOPIC_NAME)

        if client is not None:
            try:
                client.setex(cache_key, SYSTEM_LOGS_TOPIC_CACHE_TTL, str(topic_id))
            except Exception as exc:
                logger.debug(f"[Broadcaster] System Logs topic cache write failed: {exc}")

        return topic_id

    async def send_log(self, message: str):
        """Sends a log to the System Logs topic, falling back to General."""
        if not self._acquire_system_log_slot():
            logger.debug("Suppressed Telegram system log due to rate limit")
            return False

        group_id = settings.MONITOR_GROUP_ID
        topic_id: int | None = None
        try:
            topic_id = await self._resolve_system_logs_topic_id(group_id)
        except Exception as exc:
            logger.warning(
                f"[Broadcaster] System Logs topic unavailable for {group_id}; "
                f"falling back to General: {exc}"
            )

        return await self._send_control_message(
            f"🤖 [System Log]\n{message}",
            message_thread_id=topic_id,
        )

    async def send_alert(self, message: str) -> bool:
        """Sends a high-signal alert to General; never raises into callers."""
        return await self._send_control_message(
            f"🚨 [Alert]\n{message}",
            message_thread_id=GENERAL_THREAD_ID,
        )

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

    async def ensure_topic(
        self,
        group_id: int | str,
        topic_name: str,
        *,
        preferred_topic_id: int | str | None = None,
    ) -> int:
        """Resolve an existing topic by verified ID or exact title, creating only if absent."""
        if topic_name in ["General", "general", "main"]:
            return 1

        if preferred_topic_id is not None:
            try:
                preferred_id = int(preferred_topic_id)
            except (TypeError, ValueError):
                preferred_id = None
            if preferred_id:
                try:
                    preferred_title = await user_agent.get_topic_title(group_id, preferred_id)
                except UserAgentSessionUnavailableError as exc:
                    logger.warning(
                        "[Broadcaster] Could not verify saved topic %s for '%s' because "
                        "no user session is available; trusting the saved topic ID: %s",
                        preferred_id,
                        topic_name,
                        exc,
                    )
                    return preferred_id
                except Exception as exc:
                    raise RuntimeError(
                        f"Could not verify saved topic {preferred_id} for '{topic_name}': {exc}"
                    ) from exc
                if preferred_title == topic_name:
                    return preferred_id

        try:
            existing_id = await user_agent.find_topic_id(group_id, topic_name)
        except Exception as exc:
            # Do not create a duplicate topic when Telegram's current topic list
            # could not be read; retry once topic resolution is available.
            raise RuntimeError(f"Could not resolve existing topic '{topic_name}': {exc}") from exc
        if existing_id:
            return existing_id

        bot = self._get_bot_instance(self.bot_tokens[0])
        last_err = None
        for attempt in range(2):
            try:
                topic = await bot.create_forum_topic(chat_id=group_id, name=topic_name)
                await _mute_topic_best_effort(group_id, topic.message_thread_id)
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
