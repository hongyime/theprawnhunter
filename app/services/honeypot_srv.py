"""Honeypot Service — processing, message extraction, and forwarding.

Provides:
  - Extraction and normalization of incoming Telegram webhook updates into canonical
    message records for the exfiltrated_messages store.
  - Forwarding raw messages into Supabase (feeding the webapp's evidence_redacted view).
  - Triggering Telegram topic broadcasting for newly captured honeypot messages.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.database import db
from app.core.logger import get_logger
from app.domain.media import bot_api_media_info
from app.infrastructure.database import execute_async

logger = get_logger(__name__)


def _extract_media_info(target: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract media type and file metadata from a Telegram message dict."""
    return bot_api_media_info(
        target,
        extra_media_keys=("voice", "video_note", "sticker"),
    )


def _to_int(val: Any) -> int | None:
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def extract_honeypot_message(payload: dict[str, Any], credential_id: str) -> dict[str, Any] | None:
    """Normalize a raw Telegram webhook update into an exfiltrated_messages record.

    Supports:
      - message, channel_post, edited_message, edited_channel_post
      - callback_query
      - inline_query

    Uses negative update_id (-update_id) for telegram_msg_id to guarantee absolute namespace
    isolation from chat-scoped scraped message IDs (which are always positive integers >= 1).
    """
    if not isinstance(payload, dict):
        return None

    raw_uid = payload.get("update_id")
    # Strict validation: reject bool, float, strings, None, <= 0, and > 2^31 - 1
    MAX_TELEGRAM_UPDATE_ID = 2_147_483_647
    if (
        not isinstance(raw_uid, int)
        or isinstance(raw_uid, bool)
        or raw_uid <= 0
        or raw_uid > MAX_TELEGRAM_UPDATE_ID
    ):
        return None

    safe_update_id = raw_uid
    # Negative ID guarantees no collision with positive scraped chat message IDs
    telegram_msg_id = -safe_update_id
    now_iso = datetime.now(UTC).isoformat()

    # Case 1: Standard message / post / edit
    target = (
        payload.get("message")
        or payload.get("channel_post")
        or payload.get("edited_message")
        or payload.get("edited_channel_post")
    )
    if isinstance(target, dict):
        chat_msg_id = _to_int(target.get("message_id"))
        chat_obj = target.get("chat") if isinstance(target.get("chat"), dict) else {}
        chat_id = _to_int(chat_obj.get("id"))

        sender = target.get("from") if isinstance(target.get("from"), dict) else {}
        sender_name = (
            sender.get("username")
            or sender.get("first_name")
            or target.get("author_signature")
            or "Unknown"
        )
        sender_user_id = _to_int(sender.get("id"))
        content = target.get("text") or target.get("caption") or ""
        media_type, file_meta = _extract_media_info(target)

        file_meta["source"] = "bot_api"
        file_meta["update_id"] = safe_update_id
        file_meta["message_id"] = chat_msg_id
        file_meta["chat_id"] = chat_id

        return {
            "credential_id": credential_id,
            "telegram_msg_id": telegram_msg_id,
            "sender_name": str(sender_name)[:128],
            "sender_user_id": sender_user_id,
            "content": str(content)[:2000],
            "media_type": media_type,
            "file_meta": file_meta,
            "is_broadcasted": False,
            "created_at": now_iso,
        }

    # Case 2: Callback query
    cb = payload.get("callback_query")
    if isinstance(cb, dict):
        from_user = cb.get("from") if isinstance(cb.get("from"), dict) else {}
        sender_name = from_user.get("username") or from_user.get("first_name") or "Unknown"
        sender_user_id = _to_int(from_user.get("id"))
        cb_data = cb.get("data") or ""
        cb_msg = cb.get("message") if isinstance(cb.get("message"), dict) else {}
        chat_msg_id = _to_int(cb_msg.get("message_id"))
        cb_chat = cb_msg.get("chat") if isinstance(cb_msg.get("chat"), dict) else {}

        file_meta = {
            "source": "bot_api",
            "callback_id": cb.get("id"),
            "update_id": safe_update_id,
            "message_id": chat_msg_id,
            "chat_id": _to_int(cb_chat.get("id")),
        }

        return {
            "credential_id": credential_id,
            "telegram_msg_id": telegram_msg_id,
            "sender_name": str(sender_name)[:128],
            "sender_user_id": sender_user_id,
            "content": f"[Callback Query] {cb_data}"[:2000],
            "media_type": "callback_query",
            "file_meta": file_meta,
            "is_broadcasted": False,
            "created_at": now_iso,
        }

    # Case 3: Inline query
    iq = payload.get("inline_query")
    if isinstance(iq, dict):
        from_user = iq.get("from") if isinstance(iq.get("from"), dict) else {}
        sender_name = from_user.get("username") or from_user.get("first_name") or "Unknown"
        sender_user_id = _to_int(from_user.get("id"))
        query_text = iq.get("query") or ""

        file_meta = {
            "source": "bot_api",
            "inline_id": iq.get("id"),
            "update_id": safe_update_id,
        }

        return {
            "credential_id": credential_id,
            "telegram_msg_id": telegram_msg_id,
            "sender_name": str(sender_name)[:128],
            "sender_user_id": sender_user_id,
            "content": f"[Inline Query] {query_text}"[:2000],
            "media_type": "inline_query",
            "file_meta": file_meta,
            "is_broadcasted": False,
            "created_at": now_iso,
        }

    return None


async def forward_honeypot_message(payload: dict[str, Any], credential_id: str) -> dict[str, Any] | None:
    """Forward a honeypot update to exfiltrated_messages and trigger broadcast.

    Saves the extracted message into Supabase exfiltrated_messages so it is immediately
    visible to the webapp (evidence_redacted view), and queues a targeted broadcast task
    to send ONLY this honeypot message to the credential's forum topic.

    Safeguards:
      - Uses negative telegram_msg_id (-update_id) to prevent collision with scraped messages.
      - Checks existing rows so retried webhook deliveries NEVER reset is_broadcasted=False
        or trigger duplicate topic broadcasts.
    """
    if not getattr(settings, "HONEYPOT_FORWARD_RAW_MESSAGES", True):
        logger.debug("[Honeypot] Raw message forwarding disabled by configuration")
        return None

    msg_data = extract_honeypot_message(payload, credential_id)
    if not msg_data:
        return None

    # Check if this message was already recorded in exfiltrated_messages
    existing = await execute_async(
        db.table("exfiltrated_messages")
        .select("id, is_broadcasted")
        .eq("credential_id", credential_id)
        .eq("telegram_msg_id", msg_data["telegram_msg_id"])
        .limit(1)
    )

    if existing.data:
        is_already_broadcasted = bool(existing.data[0].get("is_broadcasted"))
        if is_already_broadcasted:
            logger.info(
                f"[Honeypot] Message {msg_data['telegram_msg_id']} already recorded and broadcasted — skipping duplicate dispatch"
            )
            return msg_data
        # Recorded but never broadcasted (e.g. earlier dispatch failed or the
        # broadcast task was lost): fall through and (re)dispatch below. The
        # redis nx dedup key still prevents double-sends within 300s.
        logger.info(
            f"[Honeypot] Message {msg_data['telegram_msg_id']} recorded but not broadcasted — redispatching"
        )
    else:
        # Fresh insert: preserves is_broadcasted=False
        await execute_async(db.table("exfiltrated_messages").insert(msg_data))

    # Trigger targeted honeypot message broadcast if enabled
    if getattr(settings, "HONEYPOT_BROADCAST_TO_TOPIC", True):
        try:
            from app.core.redis_srv import redis_srv

            dispatch_key = f"broadcast:dispatched:{credential_id}:{msg_data['telegram_msg_id']}"
            should_dispatch = True
            try:
                should_dispatch = bool(
                    await asyncio.to_thread(
                        redis_srv.client.set,
                        dispatch_key,
                        "1",
                        nx=True,
                        ex=300,
                    )
                )
            except Exception as _r_err:
                logger.debug(f"[Honeypot] redis dispatch dedup check skipped: {_r_err}")

            if should_dispatch:
                from app.workers.celery_app import app as celery_app

                await asyncio.to_thread(
                    celery_app.send_task,
                    "flow.broadcast_honeypot_message",
                    kwargs={
                        "credential_id": credential_id,
                        "telegram_msg_id": msg_data["telegram_msg_id"],
                    },
                )
        except Exception as task_exc:
            logger.warning(f"[Honeypot] Celery broadcast dispatch deferred: {task_exc}")

    return msg_data
