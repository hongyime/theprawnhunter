"""
Common helper functions extracted from services.
Reduces code duplication and improves maintainability.
"""
import re
from typing import Any

from app.core.logger import get_logger

logger = get_logger(__name__)

# Token validation regex (reused across scanners)
TOKEN_PATTERN = re.compile(r'(?<![A-Za-z0-9])(?:bot)?(\d{8,15}:[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])')
CHAT_ID_PATTERN = re.compile(r'(?:chat_id|chat|target|cid)[=_":\s]+([-\d]+)', re.IGNORECASE)


def is_valid_telegram_token(token_str: str) -> bool:
    """
    Strict validation to filter out Fernet strings, hashes, and junk.
    Valid Telegram token: 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

    Args:
        token_str: String to validate as Telegram bot token

    Returns:
        True if valid token format, False otherwise
    """
    try:
        # Explicit Fernet rejection (starts with gAAAA)
        if token_str.startswith("gAAAA"):
            return False

        # Must contain exactly one colon
        if ":" not in token_str or token_str.count(":") != 1:
            return False

        parts = token_str.split(":", 1)
        bot_id, secret = parts

        # Bot ID must be 8-15 digits, no leading zeros
        if not bot_id.isdigit():
            return False
        if len(bot_id) < 8 or len(bot_id) > 15:
            return False
        if len(bot_id) > 1 and bot_id.startswith("0"):
            return False

        # Secret must be exactly 35 characters (standardized — matches scanners.py)
        if len(secret) != 35:
            return False

        # Secret must only contain allowed chars (base64-ish)
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        if not all(c in allowed for c in secret):
            return False

        # Suspicious: Pure hex (likely hash collision)
        is_pure_hex = all(c in "0123456789abcdefABCDEF" for c in secret)
        if is_pure_hex:
            return False

        return True
    except Exception:
        return False


def extract_chat_id(text: str) -> str | None:
    """
    Extract chat ID from text using regex pattern.

    Args:
        text: Text to search for chat ID

    Returns:
        Chat ID string if found, None otherwise
    """
    matches = CHAT_ID_PATTERN.findall(text)
    return matches[0] if matches else None


def extract_tokens_and_chat_ids(text: str) -> list[dict[str, Any]]:
    """
    Extract both tokens and associated chat IDs from text.

    Args:
        text: Text to parse

    Returns:
        List of dicts with 'token' and 'chat_id' keys
    """
    tokens = TOKEN_PATTERN.findall(text)
    chat_ids = CHAT_ID_PATTERN.findall(text)

    results = []

    # For short text (like URLs), pair tokens with chat IDs
    if len(text) < 500:
        cid = chat_ids[0] if chat_ids else None
        for t in tokens:
            if is_valid_telegram_token(t):
                results.append({'token': t, 'chat_id': cid})
        return results

    # For larger bodies, just grab all valid tokens
    cid = chat_ids[0] if chat_ids else None
    for t in set(tokens):  # dedup
        if is_valid_telegram_token(t):
            results.append({'token': t, 'chat_id': cid})

    return results


def parse_telegram_message(message: Any) -> dict[str, Any]:
    """
    Parse a Telegram message object into standardized dict format.
    Works with both Telethon Message objects and Bot API dicts.

    Args:
        message: Telethon Message or Bot API message dict

    Returns:
        Standardized message dict
    """
    # Try Telethon Message object first
    if hasattr(message, 'id'):
        from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

        content = message.text or ""
        media_type = "text"
        file_meta = {}

        if message.media:
            if isinstance(message.media, MessageMediaPhoto):
                media_type = "photo"
                file_meta = {"wc": "photo", "id": getattr(message.media.photo, 'id', 0)}
            elif isinstance(message.media, MessageMediaDocument):
                media_type = "document"
                file_meta = {"mime": message.media.document.mime_type}
            else:
                media_type = "other"

        sender_name = "Unknown"
        if message.sender:
            if hasattr(message.sender, 'username') and message.sender.username:
                sender_name = message.sender.username
            elif hasattr(message.sender, 'first_name'):
                sender_name = message.sender.first_name

        return {
            "telegram_msg_id": message.id,
            "sender_name": sender_name,
            "content": content,
            "media_type": media_type,
            "file_meta": file_meta
        }

    # Bot API dict format
    else:
        content = message.get('text') or message.get('caption') or ""
        sender = message.get('from', {})

        media_type = "text"
        file_meta = {}
        if 'photo' in message:
            media_type = "photo"
        elif 'document' in message:
            media_type = "document"

        return {
            "telegram_msg_id": message.get('message_id'),
            "sender_name": sender.get('username') or sender.get('first_name') or "Unknown",
            "content": content,
            "media_type": media_type,
            "file_meta": file_meta,
            "chat_id": message.get('chat', {}).get('id')
        }
