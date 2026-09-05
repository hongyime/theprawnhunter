from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import require_monitor_key
from app.core.config import settings
from app.schemas.models import ScanRequest
from app.workers.celery_app import app as celery_app

router = APIRouter(
    prefix="/scan",
    tags=["Scanner"],
    dependencies=[Depends(require_monitor_key)],
)


@router.post("/trigger")
async def trigger_scan(request: ScanRequest):
    """
    Manually trigger an OSINT scan task.
    DISABLED in Production to prevent public abuse.
    Requires X-Monitor-Key header (enforced via router dependency).
    Valid sources: shodan, fofa, github, gitlab, urlscan, sourcegraph, searchcode
    """
    if settings.ENV == "production":
        raise HTTPException(
            status_code=403,
            detail="Manual triggering is disabled in production. Scheduled tasks only."
        )

    task_name = f"scanner.scan_{request.source.lower()}"

    # Valid sources — censys and hybrid removed (no tasks exist for them)
    if request.source.lower() not in ["shodan", "fofa", "github", "gitlab", "urlscan", "sourcegraph", "searchcode"]:
        raise HTTPException(
            status_code=400,
            detail="Unsupported source. Use 'shodan', 'fofa', 'github', 'gitlab', 'urlscan', 'sourcegraph', or 'searchcode'."
        )

    try:
        task = celery_app.send_task(task_name, args=[request.query])

        from app.workers.tasks.flow_tasks import get_broadcaster
        await get_broadcaster().send_log(
            f"🚀 **API Trigger**: Queued `{task_name}` for query: `{request.query}`"
        )

        return {
            "status": "triggered",
            "task_id": str(task.id),
            "source": request.source,
            "query": request.query,
        }
    except Exception:
        # Do NOT echo raw exception text — it can contain broker URLs
        # (Redis DSN with password) or other secrets.
        import logging
        logging.getLogger(__name__).exception("scan trigger failed")
        raise HTTPException(status_code=500, detail="Failed to queue task")
