"""Monitor stats resource routes."""

from .common import *

router = APIRouter()

@router.get("/stats", response_model=StatsOut)
async def get_stats():
    """Get system stats. Requires X-Monitor-Key header.

    PERF-004: cache is Redis-backed with 30 s TTL so all uvicorn workers
    share the fetch cost. Falls back to in-process compute on Redis errors.
    """
    try:
        from app.core.redis_srv import redis_srv

        cached_raw = await asyncio.to_thread(redis_srv.client.get, _STATS_REDIS_KEY)
        if cached_raw:
            try:
                import json as _json

                parsed = _json.loads(cached_raw)
                return StatsOut(**parsed)
            except Exception:
                pass  # cache corrupt, recompute
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")  # Redis unavailable, recompute

    try:
        stats = await _get_monitor_stats_async()
    except Exception as exc:
        logger.exception("monitor/stats query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc

    # Best-effort cache write.
    try:
        import json as _json

        from app.core.redis_srv import redis_srv

        await asyncio.to_thread(
            redis_srv.client.set,
            _STATS_REDIS_KEY,
            _json.dumps(stats.model_dump()),
            ex=_STATS_CACHE_TTL_SECONDS,
        )
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")

    return stats
