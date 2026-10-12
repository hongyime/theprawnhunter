"""Monitor engagement resource routes."""

from .common import *

router = APIRouter()

@router.post("/engagement/lifecycle")
async def record_engagement_lifecycle(payload: EngagementLifecycleIn):
    """Record qualified/handoff/outcome stages for voluntary owned-bot traffic.

    ``subject_reference`` is HMAC-pseudonymized in-process and never stored.
    """
    from app.services.engagement import CampaignAttribution, record_engagement_event

    try:
        metadata = {
            "entry": "monitor_api",
            "qualification_code": payload.qualification_code,
            "outcome_code": payload.outcome_code,
            "reason_code": payload.reason_code,
        }
        return await record_engagement_event(
            owned_bot_id=payload.owned_bot_id,
            subject_id=payload.subject_reference,
            event_type=payload.event_type,
            attribution=CampaignAttribution(
                payload.campaign_id, payload.campaign_source, True
            ),
            metadata={key: value for key, value in metadata.items() if value is not None},
            occurred_at=payload.occurred_at,
        )
    except Exception as exc:
        logger.exception("monitor/engagement lifecycle record failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc
