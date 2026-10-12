"""Monitor targets resource routes."""

from .common import *

router = APIRouter()

@router.get("/targets/export")
def export_targets(limit: int = 100):
    """Export a sanitized generic target feed for downstream tooling.

    The feed intentionally contains only domain/URL candidates and neutral
    provenance. It never selects or returns token material, token hashes,
    raw message content, chat metadata, credentials, or webhook probe details.
    """
    limit = max(1, min(limit, 1000))
    try:
        items = _target_feed_items(limit)
        return {
            "schema_version": "target-feed.v1",
            "generated_at": _utc_now_iso(),
            "items": items,
        }
    except Exception as exc:
        logger.exception("monitor/targets/export query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc
