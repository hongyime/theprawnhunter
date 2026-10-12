"""Telegram token and chat-ID parsing rules shared across the application."""

import re
from typing import Any

from app.core.logger import get_logger

logger = get_logger("app.utils.helpers")

# Match bare tokens and the /bot{token} URL form.
TOKEN_PATTERN = re.compile(r'(?<![A-Za-z0-9])(?:bot)?(\d{8,15}:[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])')
CHAT_ID_PATTERN = re.compile(r'(?:chat_id|chat|target|cid)[=_":\s]+([-\d]+)', re.IGNORECASE)


def is_valid_telegram_token(token_str: str) -> bool:
    """Return whether a string has the strict Telegram bot-token shape."""
    try:
        # Explicit Fernet rejection (starts with gAAAA).
        if token_str.startswith("gAAAA"):
            return False

        # Must contain exactly one colon.
        if ":" not in token_str or token_str.count(":") != 1:
            return False

        bot_id, secret = token_str.split(":", 1)

        # Bot ID must be 8-15 digits, no leading zeros.
        if not bot_id.isdigit() or not 8 <= len(bot_id) <= 15:
            return False
        if len(bot_id) > 1 and bot_id.startswith("0"):
            return False

        # Secret must be exactly 35 characters with the existing allowed set.
        if len(secret) != 35:
            return False
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        if not all(char in allowed for char in secret):
            return False

        # Pure hex values are likely hash collisions.
        is_pure_hex = all(char in "0123456789abcdefABCDEF" for char in secret)
        return not is_pure_hex
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")
        return False


# Legacy scanner import name.
_is_valid_token = is_valid_telegram_token


def extract_chat_id(text: str) -> str | None:
    """Extract the first chat ID from text, if present."""
    matches = CHAT_ID_PATTERN.findall(text)
    return matches[0] if matches else None


def extract_tokens_and_chat_ids(text: str) -> list[dict[str, Any]]:
    """Extract valid Telegram tokens and associate the first chat ID."""
    tokens = TOKEN_PATTERN.findall(text)
    chat_ids = CHAT_ID_PATTERN.findall(text)
    results = []

    if len(text) < 500:
        chat_id = chat_ids[0] if chat_ids else None
        for token in tokens:
            if is_valid_telegram_token(token):
                results.append({"token": token, "chat_id": chat_id})
        return results

    chat_id = chat_ids[0] if chat_ids else None
    for token in set(tokens):
        if is_valid_telegram_token(token):
            results.append({"token": token, "chat_id": chat_id})
    return results


__all__ = [
    "CHAT_ID_PATTERN",
    "TOKEN_PATTERN",
    "_is_valid_token",
    "extract_chat_id",
    "extract_tokens_and_chat_ids",
    "is_valid_telegram_token",
    "logger",
]
