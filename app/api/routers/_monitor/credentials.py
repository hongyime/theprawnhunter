"""Monitor credentials resource routes."""

from .common import *

router = APIRouter()

@router.get("/credentials", response_model=list[CredentialOut])
async def list_credentials(
    limit: int = 100,
    sort_by: str = "created_at",
    order: str = "desc",
):
    """List recent credentials.

    Args:
        limit: 1-1000 (clamped). Default 100.
        sort_by: one of 'created_at' (default), 'updated_at', 'collection_yield_score',
                 'confidence_score' (legacy alias for collection_yield_score),
                 'chat_member_count'. The score/member-count columns read from meta jsonb.
        order: 'desc' (default) or 'asc'.

    Requires X-Monitor-Key header.
    """
    limit = max(1, min(limit, 1000))
    desc = order.lower() != "asc"

    # Whitelist sort keys — never trust user input as a column reference.
    # confidence_score, collection_yield_score, and chat_member_count are STORED
    # generated columns (see migrations 004 + 20260903000003) so sorts are real INT,
    # not jsonb-string lex sort. confidence_score is retained as a legacy alias for
    # collection_yield_score — new code should prefer collection_yield_score.
    allowed_sorts = {
        "created_at",
        "updated_at",
        "collection_yield_score",
        "confidence_score",
        "chat_member_count",
    }
    sort_expr = sort_by if sort_by in allowed_sorts else "created_at"

    try:
        q = db.table("discovered_credentials").select("*")
        if sort_expr in ("collection_yield_score", "confidence_score", "chat_member_count"):
            try:
                # nullslast keeps unscored legacy rows out of the way on desc.
                res = await execute_async(
                    q.order(sort_expr, desc=desc, nullsfirst=not desc).limit(limit)
                )
            except Exception as e:
                # Migration 004 not applied yet — column doesn't exist. Fall back.
                msg = str(e).lower()
                if (
                    "collection_yield_score" in msg
                    or "confidence_score" in msg
                    or "chat_member_count" in msg
                    or "column" in msg
                ):
                    res = await execute_async(
                        db.table("discovered_credentials")
                        .select("*")
                        .order("created_at", desc=True)
                        .limit(limit)
                    )
                else:
                    raise
        else:
            res = await execute_async(q.order(sort_expr, desc=desc).limit(limit))
        return res.data
    except Exception as exc:
        logger.exception("monitor/credentials query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc
