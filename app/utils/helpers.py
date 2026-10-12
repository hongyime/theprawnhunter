"""Compatibility facade for legacy parser imports.

New code should import parser implementations from :mod:`app.utils.parsing`.
"""

from app.utils.parsing import (
    CHAT_ID_PATTERN,
    TOKEN_PATTERN,
    extract_chat_id,
    extract_tokens_and_chat_ids,
    is_valid_telegram_token,
    logger,
    parse_telegram_message,
)

__all__ = [
    "CHAT_ID_PATTERN",
    "TOKEN_PATTERN",
    "extract_chat_id",
    "extract_tokens_and_chat_ids",
    "is_valid_telegram_token",
    "parse_telegram_message",
]
