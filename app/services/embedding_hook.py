"""Best-effort incremental embedding hook.

Capture flows call :func:`embed_message_best_effort` immediately after a message
row is stored. The hook is *disabled by default*: it does nothing unless
``EMBED_API_URL`` is present in the environment. When enabled it posts one text
to the local ``embed-api`` service and upserts the resulting vector into
``message_embeddings``.

The embedded text is :func:`app.services.media_text.searchable_media_text`
(content + ``file_meta.transcript_text`` + ``file_meta.ocr_text``), matching the
full-text document built by the ``20261025000009_media_text_search.sql``
migration. Callers that hold the stored row should pass ``file_meta`` so ASR and
OCR text are covered by semantic search too; passing ``None`` degrades to
content-only, exactly as before.

Every failure is swallowed. Embedding must never slow down or break capture, and
a missing/down embed-api is an expected operating state (the service only runs
in the optional ``heavy`` profile).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.core.logger import get_logger
from app.services.media_text import searchable_media_text

logger = get_logger(__name__)

EMBED_API_URL_ENV = "EMBED_API_URL"
EMBED_TIMEOUT_ENV = "EMBED_API_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_EMBED_API_URL = "http://127.0.0.1:8020"
MODEL_NAME = "all-MiniLM-L6-v2"
DIMENSIONS = 384
TABLE = "message_embeddings"


def _timeout_seconds() -> float:
    raw = os.getenv(EMBED_TIMEOUT_ENV)
    try:
        value = float(raw) if raw is not None else DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def _base_url() -> str | None:
    """Return the configured embed-api URL, or None when the hook is disabled."""
    raw = os.getenv(EMBED_API_URL_ENV, "").strip()
    return raw.rstrip("/") if raw else None


def _post_embed(
    http_client: Any,
    base_url: str,
    text: str,
    *,
    timeout: float,
) -> list[float]:
    response = http_client.post(
        f"{base_url}/embed",
        json={"texts": [text]},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json() or {}
    vectors = payload.get("vectors") or []
    if len(vectors) != 1:
        raise ValueError(f"embed-api returned {len(vectors)} vectors for one text")
    vector = vectors[0]
    if len(vector) != DIMENSIONS:
        raise ValueError(f"embed-api returned {len(vector)} dimensions")
    return [float(value) for value in vector]


def _get_db() -> Any:
    from app.core.database import db

    return db


def embed_message_best_effort(
    message_id: str | None,
    content: str | None,
    *,
    file_meta: dict | None = None,
    db: Any = None,
    http_client: Any = None,
    timeout: float | None = None,
) -> bool:
    """Embed one stored message and upsert its vector. Never raises.

    The text is ``searchable_media_text(content, file_meta)`` so transcript and
    OCR survive into the vector when the caller supplies ``file_meta``. Returns
    True only when a vector was stored. Disabled hook, no searchable text, HTTP
    failure, malformed response and DB failure all return False.
    """
    text = searchable_media_text(content, file_meta)
    if not message_id or not text:
        return False

    base_url = _base_url()
    if not base_url:
        return False

    effective_timeout = timeout if timeout is not None else _timeout_seconds()
    try:
        if http_client is None:
            with httpx.Client(timeout=httpx.Timeout(effective_timeout)) as client:
                vector = _post_embed(client, base_url, text, timeout=effective_timeout)
        else:
            vector = _post_embed(http_client, base_url, text, timeout=effective_timeout)

        client_db = db if db is not None else _get_db()
        client_db.table(TABLE).upsert(
            {
                "message_id": message_id,
                "embedding": vector,
                "model": MODEL_NAME,
            },
            on_conflict="message_id",
        ).execute()
    except Exception as exc:  # noqa: BLE001 - embedding must never break capture
        logger.warning(f"[EmbedHook] skipped msg={message_id}: {exc}")
        return False

    logger.debug(f"[EmbedHook] embedded msg={message_id}")
    return True


__all__ = [
    "DEFAULT_EMBED_API_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIMENSIONS",
    "EMBED_API_URL_ENV",
    "MODEL_NAME",
    "embed_message_best_effort",
]
