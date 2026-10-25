"""Evidence thumbnails: bounded JPEG generation + Supabase Storage upload.

Contract (TODO 43 / TODO 44):
    - ``make_thumbnail`` decodes image bytes, converts to RGB, caps the longest
      side at 320 px and JPEG-encodes at <= 64 KB (quality ladder then 0.8x
      downscale rounds). Undecodable input returns ``None`` — never raises.
    - ``store_thumbnail`` uploads a generated thumbnail to the private
      ``evidence-thumbs`` bucket at ``<credential_id>/<message_id>.jpg`` using
      the service-role client (``upsert``). Any failure returns ``None``.
    - ``attach_thumbnail`` is the broadcaster seam: image media only, merges
      ``thumb_path`` into ``file_meta`` on success and leaves the key absent on
      failure. A thumbnail failure never propagates.

Media bytes and storage paths are never logged.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from PIL import Image, ImageOps

logger = logging.getLogger("thumbnails")

THUMB_BUCKET = "evidence-thumbs"
DEFAULT_MAX_DIM = 320
DEFAULT_MAX_BYTES = 64_000

# First pass at 85; then the documented retry ladder. After the ladder is
# exhausted the image is downscaled 0.8x and the ladder is retried, up to
# MAX_DOWNSCALE_ROUNDS times.
_QUALITY_LADDER = (85, 70, 55, 40)
MAX_DOWNSCALE_ROUNDS = 3
_DOWNSCALE_FACTOR = 0.8
_JPEG_CONTENT_TYPE = "image/jpeg"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")

PersistHook = Callable[[Any, dict[str, Any]], Awaitable[Any]]


def is_image_media(media_type: str, file_meta: dict[str, Any] | None) -> bool:
    """True only for photos and image documents (mirrors the OCR heuristic)."""
    if media_type == "photo":
        return True
    if media_type != "document":
        return False
    meta = file_meta or {}
    mime = str(meta.get("mime") or meta.get("mime_type") or "").lower()
    if mime.startswith("image/"):
        return True
    file_name = str(meta.get("file_name") or "").lower()
    return file_name.endswith(IMAGE_EXTENSIONS)


def _to_rgb(img: Image.Image) -> Image.Image:
    """Flatten transparency onto white so JPEG encoding cannot fail on alpha."""
    if img.mode == "RGB":
        return img
    if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def make_thumbnail(
    data: bytes,
    *,
    max_dim: int = DEFAULT_MAX_DIM,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> bytes | None:
    """Return a bounded JPEG thumbnail, or ``None`` when input is unusable."""
    if not isinstance(data, bytes | bytearray) or not data:
        return None
    try:
        with Image.open(io.BytesIO(bytes(data))) as source:
            source.load()
            try:
                oriented = ImageOps.exif_transpose(source)
            except Exception:  # noqa: BLE001 - orientation is best-effort
                oriented = source
            image = _to_rgb(oriented)
            image.thumbnail((max_dim, max_dim), Image.LANCZOS)

            if image.width < 1 or image.height < 1:
                return None

            for round_index in range(MAX_DOWNSCALE_ROUNDS + 1):
                for quality in _QUALITY_LADDER:
                    try:
                        encoded = _encode_jpeg(image, quality)
                    except Exception:  # noqa: BLE001 - unsupported pixel layouts
                        return None
                    if len(encoded) <= max_bytes:
                        return encoded
                if round_index == MAX_DOWNSCALE_ROUNDS:
                    break
                image = image.resize(
                    (
                        max(1, int(image.width * _DOWNSCALE_FACTOR)),
                        max(1, int(image.height * _DOWNSCALE_FACTOR)),
                    ),
                    Image.LANCZOS,
                )
    except Exception:  # noqa: BLE001 - Pillow cannot decode / truncated data
        return None
    return None


def thumbnail_path(credential_id: Any, message_id: Any) -> str | None:
    """Build ``<credential_id>/<message_id>.jpg``; reject path traversal."""
    credential = str(credential_id or "").strip()
    message = str(message_id or "").strip()
    if not credential or not message:
        return None
    for part in (credential, message):
        if "/" in part or "\\" in part or part in {".", ".."}:
            return None
    return f"{credential}/{message}.jpg"


def _default_client() -> Any:
    from app.core.database import Database

    return Database.get_client()


def _sanitize_options() -> dict[str, str]:
    return {"content-type": _JPEG_CONTENT_TYPE, "upsert": "true"}


async def store_thumbnail(
    data: bytes,
    *,
    credential_id: Any,
    message_id: Any,
    client: Any = None,
    max_dim: int = DEFAULT_MAX_DIM,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str | None:
    """Generate and upload a thumbnail; returns its storage path or ``None``."""
    try:
        path = thumbnail_path(credential_id, message_id)
        if not path:
            return None
        thumbnail = make_thumbnail(data, max_dim=max_dim, max_bytes=max_bytes)
        if thumbnail is None:
            return None
        storage_client = client if client is not None else _default_client()
        if storage_client is None:
            return None

        def _upload() -> Any:
            return storage_client.storage.from_(THUMB_BUCKET).upload(
                path, thumbnail, _sanitize_options()
            )

        await asyncio.to_thread(_upload)
        return path
    except Exception as exc:  # noqa: BLE001 - storage outage must not propagate
        # Deliberately no path / byte logging.
        logger.warning(f"    ⚠️ [Thumbnails] upload failed for message {message_id}: {exc.__class__.__name__}")
        return None


async def attach_thumbnail(
    msg_obj: dict[str, Any],
    media_type: str,
    data: bytes | None,
    *,
    client: Any = None,
    persist: PersistHook | None = None,
    max_dim: int = DEFAULT_MAX_DIM,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str | None:
    """Best-effort: thumbnail image media and merge ``file_meta['thumb_path']``.

    Only image media types are considered. Returns the stored path on success
    and ``None`` on skip/failure. Never raises.
    """
    try:
        file_meta = msg_obj.get("file_meta")
        if not isinstance(file_meta, dict):
            file_meta = {}
            msg_obj["file_meta"] = file_meta
        if not is_image_media(media_type, file_meta):
            return None
        if not data:
            return None

        path = await store_thumbnail(
            data,
            credential_id=msg_obj.get("credential_id"),
            message_id=msg_obj.get("id"),
            client=client,
            max_dim=max_dim,
            max_bytes=max_bytes,
        )
        if not path:
            return None

        file_meta["thumb_path"] = path
        if persist is not None:
            try:
                await persist(msg_obj.get("id"), dict(file_meta))
            except Exception as exc:  # noqa: BLE001 - persistence is best-effort
                logger.warning(f"    ⚠️ [Thumbnails] thumb_path persist failed: {exc.__class__.__name__}")
        return path
    except Exception as exc:  # noqa: BLE001 - must never break the caller
        logger.warning(f"    ⚠️ [Thumbnails] thumbnail skipped: {exc.__class__.__name__}")
        return None
