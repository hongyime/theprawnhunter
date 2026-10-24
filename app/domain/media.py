"""Telegram media metadata normalization shared by API and service callers."""

import logging
from collections.abc import Iterable
from typing import Any

from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

logger = logging.getLogger(__name__)


def bot_api_media_info(
    payload: dict[str, Any],
    *,
    extra_media_keys: Iterable[str] = (),
) -> tuple[str, dict[str, Any]]:
    """Extract Bot API media type and metadata without changing the source shape."""
    file_meta: dict[str, Any] = {"source": "bot_api"}

    if isinstance(payload.get("photo"), list) and payload["photo"]:
        photo = payload["photo"][-1] or {}
        if isinstance(photo, dict):
            _copy_if_present(file_meta, photo, "file_id")
            _copy_if_present(file_meta, photo, "file_unique_id")
            _copy_if_present(file_meta, photo, "file_size")
            _copy_if_present(file_meta, photo, "width")
            _copy_if_present(file_meta, photo, "height")
        return "photo", file_meta

    for key in ("document", "video", "audio", *extra_media_keys):
        media = payload.get(key)
        if isinstance(media, dict):
            _copy_if_present(file_meta, media, "file_id")
            _copy_if_present(file_meta, media, "file_unique_id")
            _copy_if_present(file_meta, media, "file_name")
            _copy_if_present(file_meta, media, "mime_type", "mime")
            _copy_if_present(file_meta, media, "file_size")
            return key, file_meta

    animation = payload.get("animation")
    if isinstance(animation, dict):
        _copy_if_present(file_meta, animation, "file_id")
        _copy_if_present(file_meta, animation, "file_unique_id")
        _copy_if_present(file_meta, animation, "file_name")
        _copy_if_present(file_meta, animation, "mime_type", "mime")
        _copy_if_present(file_meta, animation, "file_size")
        _copy_if_present(file_meta, animation, "duration")
        _copy_if_present(file_meta, animation, "width")
        _copy_if_present(file_meta, animation, "height")
        return "animation", file_meta

    paid_media = payload.get("paid_media")
    if isinstance(paid_media, dict):
        star_count = paid_media.get("star_count")
        if isinstance(star_count, int):
            file_meta["paid_media_star_count"] = star_count
        items = paid_media.get("paid_media_items")
        if isinstance(items, list):
            file_meta["paid_media_items"] = [
                item for item in items if isinstance(item, dict)
            ]
        return "paid_media", file_meta

    story = payload.get("story")
    if isinstance(story, dict):
        story_id = story.get("id")
        if isinstance(story_id, int):
            file_meta["story_id"] = story_id
        story_chat = story.get("chat")
        if isinstance(story_chat, dict) and isinstance(story_chat.get("id"), int):
            file_meta["story_chat_id"] = story_chat["id"]
        return "story", file_meta

    return "text", file_meta


def telethon_media_info(
    message: Any,
    *,
    include_extended_metadata: bool = False,
    event_logger: Any = None,
    photo_type: type = MessageMediaPhoto,
    document_type: type = MessageMediaDocument,
) -> tuple[str, dict[str, Any]]:
    """Extract Telethon media metadata, optionally preserving scraper file refs."""
    media = getattr(message, "media", None)
    if not media:
        return "text", {}

    file_meta: dict[str, Any] = {}
    if include_extended_metadata:
        file_meta["source"] = "telethon"

    try:
        from telethon import utils as telethon_utils

        file_id = telethon_utils.pack_bot_file_id(media)
        if file_id:
            file_meta["file_id"] = file_id
    except Exception as _swallowed:
        (event_logger or logger).debug(f"[suppressed] {_swallowed}")

    if isinstance(media, photo_type):
        photo = getattr(media, "photo", None)
        file_meta["wc"] = "photo"
        file_meta["id"] = getattr(photo, "id", 0)
        if include_extended_metadata:
            file_meta["access_hash"] = getattr(photo, "access_hash", 0)
            file_ref = getattr(photo, "file_reference", b"")
            file_meta["file_reference"] = file_ref.hex() if isinstance(file_ref, bytes) else ""
        return "photo", file_meta

    if isinstance(media, document_type):
        document = getattr(media, "document", None)
        mime = getattr(document, "mime_type", None) or getattr(
            getattr(message, "file", None), "mime_type", None
        )
        if mime:
            file_meta["mime"] = mime
        file_name = getattr(getattr(message, "file", None), "name", None)
        if file_name:
            file_meta["file_name"] = file_name
        doc_id = getattr(document, "id", None)
        if doc_id is not None:
            file_meta["id"] = doc_id
            if include_extended_metadata:
                file_meta["access_hash"] = getattr(document, "access_hash", 0)
                file_ref = getattr(document, "file_reference", b"")
                file_meta["file_reference"] = file_ref.hex() if isinstance(file_ref, bytes) else ""
        if isinstance(mime, str) and mime.startswith("video/"):
            return "video", file_meta
        if isinstance(mime, str) and mime.startswith("audio/"):
            return "audio", file_meta
        return "document", file_meta

    return "other", file_meta


def _copy_if_present(
    target: dict[str, Any],
    source: dict[str, Any],
    source_key: str,
    target_key: str | None = None,
) -> None:
    value = source.get(source_key)
    if value is not None:
        target[target_key or source_key] = value


__all__ = ["bot_api_media_info", "telethon_media_info"]
