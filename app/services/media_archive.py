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
import os
import shutil
import time
from pathlib import Path

import httpx

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

_CAPACITY_CACHE: dict[str, float] = {"checked_at": 0.0, "used": 0.0}
_CAPACITY_TTL = 30.0
_STORE_MEDIA_TYPES = {"photo", "video", "document", "audio", "animation"}


def root() -> Path:
    return Path(settings.MEDIA_ARCHIVE_DIR)


def key_for(credential_id: str, telegram_msg_id: int | str, file_id: str = "") -> str:
    digest = hashlib.sha256(str(file_id or telegram_msg_id).encode()).hexdigest()[:10]
    return f"{credential_id}/{telegram_msg_id}-{digest}"


def path_for(key: str) -> Path:
    return root() / key


def _part_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.part")


def _finalize_capture(temp: Path, target: Path, expected_size: int = 0) -> bool:
    """Promote a completed download; never expose a partial file at `target`."""
    try:
        size = temp.stat().st_size
        if size <= 0 or (expected_size and size != expected_size):
            temp.unlink(missing_ok=True)
            return False
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, target)
        return True
    except OSError as exc:
        temp.unlink(missing_ok=True)
        logger.debug(f"[MediaArchive] finalize failed for {target.name}: {exc}")
        return False


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
        _part_path(path).unlink(missing_ok=True)
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
    part_cutoff = time.time() - 3600
    for path in base.rglob("*"):
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if path.name.endswith(".part"):
                if mtime < part_cutoff:
                    path.unlink()
                    removed += 1
            elif mtime < cutoff:
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
            temp = _part_path(target)
            temp.unlink(missing_ok=True)
            written = 0
            async with client.stream(
                "GET", f"https://api.telegram.org/file/bot{token}/{remote}"
            ) as response:
                if response.status_code != 200:
                    return None
                with temp.open("wb") as handle:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > settings.MEDIA_ARCHIVE_MAX_FILE_BYTES:
                            temp.unlink(missing_ok=True)
                            logger.info("[MediaArchive] aborting capture: file exceeds cap")
                            return None
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            if not _finalize_capture(temp, target, size):
                return None
            _CAPACITY_CACHE["checked_at"] = 0.0
            return target
    except Exception as exc:  # noqa: BLE001 - capture must never break ingest
        _part_path(path_for(key)).unlink(missing_ok=True)
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
    temp = _part_path(target)
    temp.unlink(missing_ok=True)
    try:
        downloaded = await asyncio.wait_for(
            client.download_media(message, file=str(temp)), timeout=300
        )
        if not downloaded:
            return None
        source = Path(str(downloaded))
        if source != temp and source.exists():
            temp = source
        if not _finalize_capture(temp, target):
            return None
        if target.stat().st_size > settings.MEDIA_ARCHIVE_MAX_FILE_BYTES:
            target.unlink(missing_ok=True)
            return None
        _CAPACITY_CACHE["checked_at"] = 0.0
        return target
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[MediaArchive] telethon capture failed: {exc}")
        temp.unlink(missing_ok=True)
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
    temp = _part_path(target)
    temp.unlink(missing_ok=True)
    try:
        entity = chat_id
        if str(chat_id).lstrip("-").isdigit():
            entity = int(chat_id)
        message = await client.get_messages(entity, ids=int(message_id))
        if not message or not getattr(message, "media", None):
            return None
        downloaded = await asyncio.wait_for(
            client.download_media(message, file=str(temp)), timeout=300
        )
        if not downloaded:
            return None
        source = Path(str(downloaded))
        if source != temp and source.exists():
            temp = source
        if not _finalize_capture(temp, target):
            return None
        size = target.stat().st_size
        if size > settings.MEDIA_ARCHIVE_MAX_FILE_BYTES:
            target.unlink(missing_ok=True)
            logger.info(f"[MediaArchive] skipping {size // 1024**2} MB file (over per-file cap)")
            return None
        _CAPACITY_CACHE["checked_at"] = 0.0
        return target
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[MediaArchive] chat capture failed: {exc}")
        temp.unlink(missing_ok=True)
        return None


async def _persist_thumb_path(message_id, file_meta: dict) -> None:
    """Merge thumb_path into the stored file_meta without clobbering enrichment."""
    try:
        from app.core.database import db
        from app.infrastructure.database import execute_async

        merged = dict(file_meta)
        res = await execute_async(
            db.table("exfiltrated_messages").select("file_meta").eq("id", message_id).limit(1)
        )
        rows = res.data or []
        stored = rows[0].get("file_meta") if rows else None
        if isinstance(stored, dict):
            merged = {**stored, **merged}
        await execute_async(
            db.table("exfiltrated_messages").update({"file_meta": merged}).eq("id", message_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[MediaArchive] thumb persist failed: {exc}")


async def _thumb_from_cache(row: dict, media_type: str, key: str) -> None:
    """Generate the evidence thumbnail while the bytes are still on disk."""
    try:
        from app.services.thumbnails import attach_thumbnail

        path = path_for(key)
        if not path.exists() or path.stat().st_size > 25 * 1024**2:
            return
        msg_obj = {
            "id": row.get("id"),
            "credential_id": row.get("credential_id"),
            "file_meta": row.get("file_meta") or {},
        }
        await attach_thumbnail(
            msg_obj, media_type, path.read_bytes(), persist=_persist_thumb_path
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[MediaArchive] thumbnail skipped: {exc}")


async def capture_bot_rows(
    rows, bot_token: str, credential_id: str, limit: int = 25
) -> int:
    """Capture Bot API media for freshly ingested rows (and thumbnail it)."""
    captured = 0
    for row in rows:
        if captured >= limit:
            break
        file_meta = row.get("file_meta") or {}
        file_id = file_meta.get("file_id")
        media_type = row.get("media_type")
        msg_id = row.get("telegram_msg_id")
        if not file_id or media_type not in _STORE_MEDIA_TYPES or msg_id is None:
            continue
        ok, reason = capacity_status()
        if not ok:
            logger.info(f"[MediaArchive] capture paused ({reason})")
            break
        key = key_for(credential_id, msg_id, file_id)
        if path_for(key).exists():
            continue
        if await capture_from_bot(bot_token, file_id, key):
            captured += 1
            await _thumb_from_cache(row, media_type, key)
    if captured:
        logger.info(f"[MediaArchive] captured {captured} file(s) for {credential_id}")
    return captured
