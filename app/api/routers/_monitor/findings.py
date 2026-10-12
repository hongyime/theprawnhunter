"""Monitor findings resource routes."""

from .common import *

router = APIRouter()

@router.get("/findings", response_model=list[FindingOut])
def list_findings(
    finding_type: FindingType | None = Query(None),
    status: FindingStatus | None = Query(None),
    min_priority: int = Query(1, ge=1, le=10),
    limit: int = Query(100, ge=1, le=500),
):
    """Return the priority-first Insight Queue without raw message content."""
    try:
        query = (
            db.table("findings")
            .select("*")
            .gte("priority", min_priority)
        )
        if finding_type:
            query = query.eq("type", finding_type)
        if status:
            query = query.eq("status", status)
        else:
            query = query.in_("status", ["new", "triaged", "in_progress"])
        result = (
            query.order("priority", desc=True)
            .order("last_material_change_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.exception("monitor/findings query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc

@router.get(
    "/findings/{finding_id}/evidence",
    response_model=list[FindingEvidenceOut],
)
def get_finding_evidence(finding_id: UUID, limit: int = Query(200, ge=1, le=500)):
    """Return redacted provenance for a finding; raw content stays in drill-down."""
    try:
        return _finding_evidence_rows(finding_id, limit)
    except Exception as exc:
        logger.exception("monitor/findings evidence query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc

@router.get("/findings/{finding_id}", response_model=FindingDetailOut)
def get_finding(finding_id: UUID):
    """Return one finding with bounded redacted evidence."""
    try:
        result = (
            db.table("findings")
            .select("*")
            .eq("id", str(finding_id))
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="Finding not found")
        return {**rows[0], "evidence": _finding_evidence_rows(finding_id)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("monitor/findings detail query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc

@router.post(
    "/findings/{finding_id}/feedback",
    response_model=FindingFeedbackOut,
)
def record_finding_feedback_api(
    finding_id: UUID,
    payload: FindingFeedbackIn,
    actor_id: UUID = Depends(require_monitor_key),
):
    """Atomically append analyst feedback and apply its optional disposition."""
    try:
        result = db.rpc(
            "record_finding_feedback_service",
            {
                "p_actor_id": str(actor_id),
                "p_finding_id": str(finding_id),
                "p_label": payload.label,
                "p_reason_code": payload.reason_code,
                "p_note": payload.note,
                "p_status": payload.status,
                "p_assignee": payload.assignee,
                "p_suppress_pattern": payload.suppress_pattern,
            },
        ).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Finding not found")
        return {
            "feedback_id": result.data,
            "finding_id": finding_id,
            "status": payload.status,
        }
    except HTTPException:
        raise
    except Exception as exc:
        if "finding not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="Finding not found") from exc
        logger.exception("monitor/findings feedback failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc
