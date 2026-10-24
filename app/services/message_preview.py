"""Explicit previews for new captures; full text remains in the existing evidence file."""

from dataclasses import dataclass
from typing import Final, TypedDict

# Version 2: no stored-content cap. The full text of every capture is stored.
# Telegram's 4096-character message limit is handled at send time in
# broadcaster_srv (split into numbered parts), not by truncating stored data.
TEXT_PREVIEW_VERSION: Final = 2

# Retained for legacy callers; new rows never truncate.
TRUNCATION_NOTICE: Final = "[Preview only: text shortened. Full text is in the evidence JSON file.]"


class StoredTextMetadata(TypedDict):
    text_preview_version: int
    content_truncated: bool
    content_original_length: int


@dataclass(frozen=True, slots=True)
class TextPreview:
    text: str
    was_truncated: bool = False


def prepare_stored_content(text: str) -> tuple[str, StoredTextMetadata]:
    """Store the FULL text of a capture, untruncated."""
    return text, {
        "text_preview_version": TEXT_PREVIEW_VERSION,
        "content_truncated": False,
        "content_original_length": len(text),
    }


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def preview_text(content: TextPreview, limit: int) -> str:
    """Compatibility helper. New callers should not truncate at all."""
    if not content.was_truncated and utf16_length(content.text) <= limit:
        return content.text
    notice = "\n\n" + TRUNCATION_NOTICE
    available = max(0, limit - utf16_length(notice))
    visible = content.text.encode("utf-16-le")[:available * 2].decode("utf-16-le", errors="ignore")
    return visible + notice
