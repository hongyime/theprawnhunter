"""Monitor webhooks resource routes."""

from .common import *

router = APIRouter()

@router.get("/webhooks")
async def list_captured_webhooks(limit: int = 200):
    """List credentials with a captured webhook URL (someone else's C2 / research endpoint).

    Surfaces `meta.webhook_url` and related fields that `validation_tasks.py` records
    when it hits a bot with an active webhook. Useful for OSINT pivoting on third-party
    infrastructure hosting the stolen tokens.

    Requires X-Monitor-Key header.
    """
    limit = max(1, min(limit, 1000))
    try:
        # Fetch a bounded slice; population is small (~2k credentials) so
        # a Python-side filter on meta->>webhook_url is cheap.
        res = await execute_async(
            db.table("discovered_credentials")
            .select("id, bot_username, bot_id, chat_name, status, meta, created_at, updated_at")
            .order("created_at", desc=True)
            .limit(2000)
        )
        out = []
        for row in res.data or []:
            meta = row.get("meta") or {}
            webhook_url = meta.get("webhook_url")
            if not webhook_url:
                continue
            out.append(
                {
                    "credential_id": row.get("id"),
                    "bot_username": row.get("bot_username"),
                    "bot_id": row.get("bot_id"),
                    "chat_name": row.get("chat_name"),
                    "status": row.get("status"),
                    "webhook_url": webhook_url,
                    "webhook_last_error": meta.get("webhook_last_error"),
                    "webhook_pending_updates": meta.get("webhook_pending_update_count"),
                    "webhook_ip_address": meta.get("webhook_ip_address"),
                    "webhook_captured_at": meta.get("webhook_captured_at"),
                    "webhook_probe": meta.get("webhook_probe"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
            if len(out) >= limit:
                break
        return out
    except Exception as exc:
        logger.exception("monitor/webhooks query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc
