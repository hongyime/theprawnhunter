"""Telegram message parsing with token and chat-ID compatibility exports."""
from typing import Any

from app.domain.tokens import (
    CHAT_ID_PATTERN,
    TOKEN_PATTERN,
    extract_chat_id,
    extract_tokens_and_chat_ids,
    is_valid_telegram_token,
    logger,
)


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
