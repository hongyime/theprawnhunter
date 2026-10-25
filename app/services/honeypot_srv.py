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
from app.services.honeypot_urls import extract_message_urls
from app.services.message_preview import prepare_stored_content
from app.services.realtime_srv import publish_evidence_delta

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


def _is_own_monitor_group(chat: dict[str, Any]) -> bool:
    """Return True when a webhook update originates from our own monitor group.

    Captured bots added to the monitor supergroup receive its service messages;
    ingesting them would re-broadcast our own group back into itself.
    """
    configured = str(getattr(settings, "MONITOR_GROUP_ID", "") or "").strip()
    if not configured:
        return False

    chat_id = _to_int(chat.get("id"))
    if configured.lstrip("-").isdigit():
        identified = bool(str(chat.get("title") or "").strip() or chat.get("username"))
        return identified and chat_id is not None and str(chat_id) == configured

    handle = configured.lstrip("@").lower()
    username = str(chat.get("username") or "").strip().lstrip("@").lower()
    return bool(handle) and username == handle


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
        if _is_own_monitor_group(chat_obj):
            logger.debug(
                f"[Honeypot] Ignoring update {safe_update_id} from the monitor group"
            )
            return None

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
        url_metadata = extract_message_urls(target)
        if url_metadata["urls"]:
            file_meta.update(url_metadata)

        file_meta["source"] = "bot_api"
        file_meta["update_id"] = safe_update_id
        file_meta["message_id"] = chat_msg_id
        file_meta["chat_id"] = chat_id
        file_meta["source_chat_type"] = chat_obj.get("type")
        file_meta["source_chat_title"] = (
            chat_obj.get("title") or chat_obj.get("username") or chat_obj.get("first_name")
        )
        stored_content, text_metadata = prepare_stored_content(str(content))
        file_meta.update(text_metadata)

        return {
            "credential_id": credential_id,
            "telegram_msg_id": telegram_msg_id,
            "sender_name": str(sender_name)[:128],
            "sender_user_id": sender_user_id,
            "content": stored_content,
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
        if _is_own_monitor_group(cb_chat):
            logger.debug(
                f"[Honeypot] Ignoring callback update {safe_update_id} from the monitor group"
            )
            return None

        file_meta = {
            "source": "bot_api",
            "callback_id": cb.get("id"),
            "update_id": safe_update_id,
            "message_id": chat_msg_id,
            "chat_id": _to_int(cb_chat.get("id")),
            "source_chat_type": cb_chat.get("type"),
            "source_chat_title": (
                cb_chat.get("title") or cb_chat.get("username") or cb_chat.get("first_name")
            ),
        }
        stored_content, text_metadata = prepare_stored_content(f"[Callback Query] {cb_data}")
        file_meta.update(text_metadata)

        return {
            "credential_id": credential_id,
            "telegram_msg_id": telegram_msg_id,
            "sender_name": str(sender_name)[:128],
            "sender_user_id": sender_user_id,
            "content": stored_content,
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
        stored_content, text_metadata = prepare_stored_content(f"[Inline Query] {query_text}")
        file_meta.update(text_metadata)

        return {
            "credential_id": credential_id,
            "telegram_msg_id": telegram_msg_id,
            "sender_name": str(sender_name)[:128],
            "sender_user_id": sender_user_id,
            "content": stored_content,
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

    inserted = await execute_async(
        db.table("exfiltrated_messages").upsert(
            msg_data,
            on_conflict="credential_id,telegram_msg_id",
            ignore_duplicates=True,
        )
    )

    stored_row_id: str | None = None
    if inserted.data:
        first_row = inserted.data[0]
        if isinstance(first_row, dict):
            stored_row_id = first_row.get("id")

    if not inserted.data:
        existing = await execute_async(
            db.table("exfiltrated_messages")
            .select("id, is_broadcasted")
            .eq("credential_id", credential_id)
            .eq("telegram_msg_id", msg_data["telegram_msg_id"])
            .limit(1)
        )
        if not existing.data:
            logger.warning(
                f"[Honeypot] Message {msg_data['telegram_msg_id']} upsert conflicted but row is not visible yet — skipping dispatch"
            )
            return msg_data
        if bool(existing.data[0].get("is_broadcasted")):
            logger.info(
                f"[Honeypot] Message {msg_data['telegram_msg_id']} already recorded and broadcasted — skipping duplicate dispatch"
            )
            return msg_data
        stored_row_id = existing.data[0].get("id")
        logger.info(
            f"[Honeypot] Message {msg_data['telegram_msg_id']} recorded but not broadcasted — redispatching"
        )

    # Notify dashboard operators that the newest evidence page has changed.
    # Best-effort: a realtime outage must never interrupt capture/forwarding.
    if stored_row_id:
        try:
            await asyncio.to_thread(
                publish_evidence_delta,
                credential_id,
                str(stored_row_id),
            )
        except Exception as realtime_exc:
            logger.debug(f"[Honeypot] Realtime evidence delta skipped: {realtime_exc}")

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

    if stored_row_id:
        try:
            from app.workers.tasks.flows.heavy import enqueue_enrichment_for_message

            await asyncio.to_thread(
                enqueue_enrichment_for_message,
                msg_data.get("media_type"),
                stored_row_id,
                credential_id,
                msg_data.get("file_meta"),
            )
        except Exception as enqueue_exc:
            logger.warning(
                f"[Honeypot] Heavy enrichment enqueue skipped: {enqueue_exc}"
            )

        try:
            from app.services.embedding_hook import embed_message_best_effort

            await asyncio.to_thread(
                embed_message_best_effort,
                stored_row_id,
                msg_data.get("content"),
                file_meta=msg_data.get("file_meta"),
            )
        except Exception as embed_exc:
            logger.warning(f"[Honeypot] Embedding hook skipped: {embed_exc}")

        try:
            from app.services.watchlist_alerts import alert_on_matches

            await alert_on_matches(
                msg_data.get("content") or "",
                message_id=stored_row_id,
                credential_id=credential_id,
            )
        except Exception as alert_exc:
            logger.warning(f"[Honeypot] Watchlist alert skipped: {alert_exc}")

    return msg_data
