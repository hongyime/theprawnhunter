"""Relay note/sticker files without transcription, retaining no temporary files."""

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Final, Literal, TypeGuard, assert_never

import anyio
from telegram import Bot, LinkPreviewOptions, Message, ReplyParameters

from app.core.config import settings
from app.services._user_agent.common import ARCHIVE_TMP_DIR, ARCHIVE_TMP_PREFIX

NOTE_MEDIA_TYPES: Final = frozenset({"voice", "video_note", "sticker"})
NoteKind = Literal["voice", "video_note", "sticker"]


def is_note_media_type(value: str) -> TypeGuard[NoteKind]:
    return value in NOTE_MEDIA_TYPES


@dataclass(frozen=True, slots=True)
class NoteTransfer:
    kind: NoteKind
    credential_id: str | None
    file_id: str
    chat_id: int | str
    thread_id: int
    summary: str
    has_urls: bool = False


@dataclass(frozen=True, slots=True)
class NoteTransferResult:
    media: Message
    summary: Message


@dataclass(frozen=True, slots=True)
class NoteMediaTooLargeError(RuntimeError):
    size: int
    limit: int

    def __str__(self) -> str:
        return f"Attachment is {self.size} bytes; limit is {self.limit} bytes."


async def relay_note_media(source: Bot, target: Bot, transfer: NoteTransfer) -> NoteTransferResult:
    limit = settings.MAX_ARCHIVE_SIZE_MB * 1024 * 1024
    with anyio.fail_after(settings.ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS):
        remote = await source.get_file(transfer.file_id)
    if remote.file_size is not None and remote.file_size > limit:
        raise NoteMediaTooLargeError(remote.file_size, limit)

    suffix = {"voice": ".ogg", "video_note": ".mp4", "sticker": ".webp"}[transfer.kind]
    if transfer.kind == "sticker":
        remote_suffix = PurePosixPath(remote.file_path or "").suffix.lower()
        if remote_suffix in {".webp", ".png", ".tgs", ".webm"}:
            suffix = remote_suffix

    with NamedTemporaryFile(
        dir=ARCHIVE_TMP_DIR, prefix=f"{ARCHIVE_TMP_PREFIX}note_{os.getpid()}_", suffix=suffix
    ) as temporary:
        path = Path(temporary.name)
        with anyio.fail_after(settings.ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS):
            await remote.download_to_drive(custom_path=path)
        if path.stat().st_size > limit:
            raise NoteMediaTooLargeError(path.stat().st_size, limit)

        with anyio.fail_after(settings.ARCHIVE_UPLOAD_TIMEOUT_SECONDS):
            match transfer.kind:
                case "voice":
                    media = await target.send_voice(
                        transfer.chat_id, path, message_thread_id=transfer.thread_id
                    )
                case "video_note":
                    media = await target.send_video_note(
                        transfer.chat_id, path, message_thread_id=transfer.thread_id
                    )
                case "sticker":
                    media = await target.send_sticker(
                        transfer.chat_id, path, message_thread_id=transfer.thread_id
                    )
                case unreachable:
                    assert_never(unreachable)

            # Video notes and stickers cannot carry captions; keep the header/note as a reply.
            summary = await target.send_message(
                transfer.chat_id,
                transfer.summary,
                message_thread_id=transfer.thread_id,
                reply_parameters=ReplyParameters(media.message_id),
                link_preview_options=LinkPreviewOptions(is_disabled=True) if transfer.has_urls else None,
            )
    return NoteTransferResult(media, summary)
