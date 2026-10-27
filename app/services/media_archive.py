"""Bounded, self-cleaning media cache.

Media is only reliably downloadable while the source message is alive, so the
bytes are captured when the message is ingested and deleted again as soon as
the broadcast has been delivered. Nothing here is a permanent store: the cache
holds a file only for the delivery window, and an hourly sweep removes files
left behind by crashes or abandoned batches.

Two hard limits protect the host:
  * total cache size  (MEDIA_ARCHIVE_MAX_BYTES)
  * free disk floor   (MEDIA_ARCHIVE_MIN_FREE_BYTES)
When either is crossed, capture simply stops. Delivery keeps working through
the live relay path and the text fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from pathlib import Path

import httpx

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

_CAPACITY_CACHE: dict[str, float] = {"checked_at": 0.0, "used": 0.0}
_CAPACITY_TTL = 30.0


def root() -> Path:
    return Path(settings.MEDIA_ARCHIVE_DIR)


def key_for(credential_id: str, telegram_msg_id: int | str, file_id: str = "") -> str:
    digest = hashlib.sha256(str(file_id or telegram_msg_id).encode()).hexdigest()[:10]
    return f"{credential_id}/{telegram_msg_id}-{digest}"


def path_for(key: str) -> Path:
    return root() / key


def _dir_bytes() -> int:
    total = 0
    base = root()
    if not base.exists():
        return 0
    for path in base.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def used_bytes(ttl: float = _CAPACITY_TTL) -> int:
    now = time.time()
    if now - _CAPACITY_CACHE["checked_at"] < ttl:
        return int(_CAPACITY_CACHE["used"])
    total = _dir_bytes()
    _CAPACITY_CACHE.update(checked_at=now, used=float(total))
    return total


def capacity_status() -> tuple[bool, str]:
    """True when there is room for another capture."""
    if not settings.MEDIA_ARCHIVE_ENABLED:
        return False, "disabled"
    try:
        base = root()
        base.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(base).free
    except OSError as exc:
        return False, f"archive dir unavailable: {exc}"
    if free < settings.MEDIA_ARCHIVE_MIN_FREE_BYTES:
        return False, f"free disk {free // 1024**2} MB below floor"
    if used_bytes() >= settings.MEDIA_ARCHIVE_MAX_BYTES:
        return False, "cache size limit reached"
    return True, "ok"


def delete(key: str) -> bool:
    path = path_for(key)
    try:
        if path.exists():
            path.unlink()
            _CAPACITY_CACHE["checked_at"] = 0.0
            return True
    except OSError as exc:
        logger.debug(f"[MediaArchive] delete failed for {key}: {exc}")
    return False


def sweep(max_age_hours: int | None = None) -> int:
    """Remove cache files left behind by crashed or abandoned deliveries."""
    horizon = (max_age_hours if max_age_hours is not None else settings.MEDIA_ARCHIVE_RETENTION_HOURS) * 3600
    cutoff = time.time() - horizon
    removed = 0
    base = root()
    if not base.exists():
        return 0
    for path in base.rglob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        _CAPACITY_CACHE["checked_at"] = 0.0
    return removed


async def capture_from_bot(token: str, file_id: str, key: str) -> Path | None:
    """Download a Bot API file into the cache, respecting every limit."""
    ok, reason = capacity_status()
    if not ok:
        logger.info(f"[MediaArchive] capture skipped ({reason})")
        return None
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            meta = (
                await client.get(
                    f"https://api.telegram.org/bot{token}/getFile",
                    params={"file_id": file_id},
                )
            ).json()
            if not meta.get("ok"):
                logger.debug(f"[MediaArchive] getFile rejected: {meta.get('description')}")
                return None
            size = int(meta["result"].get("file_size") or 0)
            if size and size > settings.MEDIA_ARCHIVE_MAX_FILE_BYTES:
                logger.info(f"[MediaArchive] skipping {size // 1024**2} MB file (over per-file cap)")
                return None
            remote = meta["result"]["file_path"]
            target = path_for(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            async with client.stream(
                "GET", f"https://api.telegram.org/file/bot{token}/{remote}"
            ) as response:
                if response.status_code != 200:
                    return None
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > settings.MEDIA_ARCHIVE_MAX_FILE_BYTES:
                            handle.close()
                            target.unlink(missing_ok=True)
                            logger.info("[MediaArchive] aborting capture: file exceeds cap")
                            return None
                        handle.write(chunk)
            _CAPACITY_CACHE["checked_at"] = 0.0
            return target
    except Exception as exc:  # noqa: BLE001 - capture must never break ingest
        logger.debug(f"[MediaArchive] capture failed: {exc}")
        return None


async def capture_from_telethon(client, message, key: str) -> Path | None:
    """Download a Telethon message's media into the cache."""
    ok, reason = capacity_status()
    if not ok:
        logger.info(f"[MediaArchive] capture skipped ({reason})")
        return None
    target = path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        downloaded = await asyncio.wait_for(
            client.download_media(message, file=str(target)), timeout=300
        )
        if not downloaded:
            return None
        if target.exists() and target.stat().st_size > settings.MEDIA_ARCHIVE_MAX_FILE_BYTES:
            target.unlink(missing_ok=True)
            return None
        _CAPACITY_CACHE["checked_at"] = 0.0
        return target
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[MediaArchive] telethon capture failed: {exc}")
        target.unlink(missing_ok=True)
        return None


async def capture_from_chat(client, chat_id, message_id: int, key: str) -> Path | None:
    """Download a message's media over MTProto.

    The Bot API refuses files over 20 MB. MTProto has no such wall, so this is
    the path for large attachments and for messages that were captured without
    a Bot API ``file_id``.
    """
    ok, reason = capacity_status()
    if not ok:
        logger.info(f"[MediaArchive] capture skipped ({reason})")
        return None
    target = path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        entity = chat_id
        if str(chat_id).lstrip("-").isdigit():
            entity = int(chat_id)
        message = await client.get_messages(entity, ids=int(message_id))
        if not message or not getattr(message, "media", None):
            return None
        downloaded = await asyncio.wait_for(
            client.download_media(message, file=str(target)), timeout=300
        )
        if not downloaded:
            return None
        size = target.stat().st_size if target.exists() else 0
        if size > settings.MEDIA_ARCHIVE_MAX_FILE_BYTES:
            target.unlink(missing_ok=True)
            logger.info(f"[MediaArchive] skipping {size // 1024**2} MB file (over per-file cap)")
            return None
        _CAPACITY_CACHE["checked_at"] = 0.0
        return target
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[MediaArchive] chat capture failed: {exc}")
        target.unlink(missing_ok=True)
        return None
