"""Membership change processing for captured bot chats."""

from typing import Any

from app.core.config import settings
from app.core.database import db
from app.core.logger import get_logger
from app.infrastructure.database import execute_async

logger = get_logger(__name__)

_MEMBER_STATUSES = ("member", "administrator", "creator")
_LEFT_STATUSES = ("left", "kicked")


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_monitor_group(chat_id: Any) -> bool:
    monitor = _to_int(settings.MONITOR_GROUP_ID)
    if monitor is None:
        return False
    return _to_int(chat_id) == monitor


def extract_membership_update(payload: dict, credential_id: str) -> dict | None:
    my_chat_member = payload.get("my_chat_member")
    if not isinstance(my_chat_member, dict):
        return None
    chat = my_chat_member.get("chat") if isinstance(my_chat_member.get("chat"), dict) else {}
    chat_id = _to_int(chat.get("id"))
    if chat_id is None or _is_monitor_group(chat_id):
        return None
    new_status = (my_chat_member.get("new_chat_member") or {}).get("status")
    old_status = (my_chat_member.get("old_chat_member") or {}).get("status")
    if new_status in _MEMBER_STATUSES:
        action = "joined"
    elif new_status in _LEFT_STATUSES:
        action = "left"
    else:
        action = "status_change"
    return {
        "action": action,
        "chat_id": chat_id,
        "chat_type": chat.get("type"),
        "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name"),
        "new_status": new_status,
        "old_status": old_status,
        "date": my_chat_member.get("date"),
    }


def build_all_chats_merge(current_meta: dict, update: dict) -> dict:
    from datetime import UTC, datetime

    meta = dict(current_meta or {})
    all_chats = [entry for entry in meta.get("all_chats", []) if isinstance(entry, dict)]
    chat_id = update.get("chat_id")
    existing = next((entry for entry in all_chats if entry.get("chat_id") == chat_id), None)
    now = datetime.now(UTC).isoformat()
    if existing is None:
        entry = {
            "chat_id": chat_id,
            "chat_name": update.get("chat_title"),
            "chat_type": update.get("chat_type"),
            "first_seen_at": now,
            "last_seen_at": now,
            "status": "removed" if update.get("action") == "left" else "present",
        }
        all_chats.append(entry)
    else:
        existing["last_seen_at"] = now
        if update.get("chat_title") is not None:
            existing["chat_name"] = update.get("chat_title")
        if update.get("chat_type") is not None:
            existing["chat_type"] = update.get("chat_type")
        if update.get("action") == "left":
            existing["status"] = "removed"
            existing["removed_at"] = now
        else:
            existing["status"] = "present"
            existing.pop("removed_at", None)
    meta["all_chats"] = all_chats
    meta["membership_updated_at"] = now
    return meta


async def process_membership_update(payload: dict, credential_id: str) -> dict | None:
    update = extract_membership_update(payload, credential_id)
    if update is None:
        return None
    try:
        res = await execute_async(
            db.table("discovered_credentials")
            .select("meta, chat_id, chat_name, chat_type")
            .eq("id", credential_id)
            .limit(1)
        )
        rows = res.data or []
        if not rows:
            return {"status": "not_found", "action": update["action"]}
        row = rows[0]
        merged_meta = build_all_chats_merge(row.get("meta") or {}, update)
        patch = {"meta": merged_meta}
        if not row.get("chat_id"):
            patch["chat_id"] = update["chat_id"]
        if not row.get("chat_name") and update.get("chat_title"):
            patch["chat_name"] = update["chat_title"]
        if not row.get("chat_type") and update.get("chat_type"):
            patch["chat_type"] = update["chat_type"]
        await execute_async(
            db.table("discovered_credentials").update(patch).eq("id", credential_id)
        )
        logger.info(
            f"[Membership] {update['action']} chat_id={update['chat_id']} "
            f"({update.get('chat_type')}) for credential {credential_id[:8]}"
        )
        return {"status": "ok", "action": update["action"], "chat_id": update["chat_id"]}
    except Exception as exc:
        logger.warning(f"[Membership] processing failed for {credential_id[:8]}: {exc}")
        return {"status": "error", "error": str(exc)[:300]}
