"""Keep honeypot update IDs separate from Telegram attachment source IDs."""

from collections.abc import Mapping

from app.services.honeypot_rendering import JsonValue


def is_honeypot_message(record: Mapping[str, JsonValue]) -> bool:
    identifier = record.get("telegram_msg_id")
    return isinstance(identifier, int) and identifier < 0


def archive_message_id(record: Mapping[str, JsonValue]) -> int | None:
    identifier = record.get("telegram_msg_id")
    if is_honeypot_message(record):
        metadata = record.get("file_meta")
        identifier = metadata.get("message_id") if isinstance(metadata, dict) else None
    if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0:
        return identifier
    return None


def honeypot_source_chat_id(record: Mapping[str, JsonValue]) -> int | None:
    metadata = record.get("file_meta")
    identifier = metadata.get("chat_id") if isinstance(metadata, dict) else None
    if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier != 0:
        return identifier
    return None
