"""Shared searchable text for captured evidence rows.

A message's searchable surface is more than its caption: ASR writes
``file_meta.transcript_text`` and OCR writes ``file_meta.ocr_text``. Full-text
search and vector search must cover exactly the same words, so both call
:func:`searchable_media_text` to build the text they index/embed.

SQL parity
----------
``supabase/migrations/20261025000009_media_text_search.sql`` derives the FTS
document with::

    coalesce(content, '') || ' ' ||
    coalesce(file_meta->>'transcript_text', '') || ' ' ||
    coalesce(file_meta->>'ocr_text', '')

This helper emits the same logical parts (content, transcript, OCR) joined by
newlines. Newline vs. space is irrelevant to ``to_tsvector('simple', ...)``:
both are whitespace separators, so indexed lexemes match the embedded text.
Keep the two expressions in sync if a new derived key is added.

The helper is defensive by contract: it never raises, ignores non-string /
missing values, and truncates to a bounded size so a pathological transcript
cannot blow up an embed-api request or a DB round trip.
"""

from __future__ import annotations

from typing import Any

# Hard cap shared by FTS/embedding callers. 100k characters is far above any
# realistic caption + transcript + OCR combination while still bounding cost.
MAX_SEARCHABLE_MEDIA_TEXT_CHARS = 100_000
SEARCHABLE_MEDIA_TEXT_KEYS = ("transcript_text", "ocr_text")


def _clean_text(value: Any) -> str:
    """Return a stripped string, or "" for anything that is not a string."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def searchable_media_text(content: str | None, file_meta: dict | None) -> str:
    """Build the searchable/embeddable text for one evidence row.

    Returns ``content`` followed by ``file_meta.transcript_text`` and
    ``file_meta.ocr_text`` (when present and non-blank), separated by newlines.
    Missing/empty/non-string values are skipped. Never raises; the result is
    at most :data:`MAX_SEARCHABLE_MEDIA_TEXT_CHARS` characters. Returns ``""``
    when there is nothing searchable.
    """
    parts: list[str] = []

    text = _clean_text(content)
    if text:
        parts.append(text)

    if isinstance(file_meta, dict):
        for key in SEARCHABLE_MEDIA_TEXT_KEYS:
            text = _clean_text(file_meta.get(key))
            if text:
                parts.append(text)

    if not parts:
        return ""
    return "\n".join(parts)[:MAX_SEARCHABLE_MEDIA_TEXT_CHARS]


__all__ = [
    "MAX_SEARCHABLE_MEDIA_TEXT_CHARS",
    "SEARCHABLE_MEDIA_TEXT_KEYS",
    "searchable_media_text",
]
