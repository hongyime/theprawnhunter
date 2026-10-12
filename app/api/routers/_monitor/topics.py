"""Monitor topics resource routes."""

from .common import *

router = APIRouter()

@router.post("/topics/revoked/close")
async def close_revoked_topics(limit: int = 50, dry_run: bool = True, dispatch: bool = True):
    """Close forum topics for revoked credentials. Defaults to dry-run."""
    limit = max(1, min(limit, 500))
    if dry_run or not dispatch:
        try:
            from app.services.topic_admin_srv import close_revoked_topics_logic

            return await close_revoked_topics_logic(
                limit=limit,
                dry_run=dry_run,
                actor="monitor_api",
                force=True,
            )
        except Exception as exc:
            logger.exception("monitor/topics/revoked/close inline run failed")
            raise HTTPException(status_code=500, detail="Internal error") from exc

    try:
        from app.workers.celery_app import app as celery_app

        task = celery_app.send_task(
            "flow.close_revoked_topics",
            kwargs={"limit": limit, "dry_run": False, "force": True},
        )
        return {
            "status": "dispatched",
            "dry_run": False,
            "limit": limit,
            "task_id": task.id,
        }
    except Exception as exc:
        logger.exception("monitor/topics/revoked/close dispatch failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc
