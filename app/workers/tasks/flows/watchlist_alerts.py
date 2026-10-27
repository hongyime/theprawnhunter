"""Drain watchlist hits that could not reach the Watchlist Hits topic.

The topic id is resolved through a user session, which is unavailable while
sessions are flood-waited. Rather than posting hits to General (wrong topic),
``send_watchlist_alert`` queues them here and this task retries.
"""

from __future__ import annotations

import json

from .common import *

MAX_ATTEMPTS = 20


@app.task(
    name="flow.drain_watchlist_alerts",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=2,
)
def drain_watchlist_alerts(limit: int = 50):
    return _run_sync(_drain_watchlist_alerts_async(limit))


async def _drain_watchlist_alerts_async(limit: int = 50):
    from app.core.redis_srv import redis_srv
    from app.services.broadcaster_srv import (
        WATCHLIST_ALERT_RETRY_KEY,
        BroadcasterService,
        enqueue_watchlist_alert,
    )

    broadcaster = BroadcasterService()
    sent = requeued = dropped = 0
    for _ in range(max(1, min(limit, 200))):
        raw = redis_srv.client.rpop(WATCHLIST_ALERT_RETRY_KEY)
        if not raw:
            break
        try:
            payload = json.loads(raw)
        except Exception:
            dropped += 1
            continue
        message = payload.get("message") or ""
        attempts = int(payload.get("attempts") or 0) + 1
        if not message:
            dropped += 1
            continue
        try:
            ok = await broadcaster.send_watchlist_alert(message, queue_on_failure=False)
        except Exception as exc:
            logger.warning(f"[WatchlistAlerts] send raised: {exc}")
            ok = False
        if ok:
            sent += 1
        elif attempts < MAX_ATTEMPTS:
            enqueue_watchlist_alert(message, attempts=attempts)
            requeued += 1
        else:
            logger.error(
                f"[WatchlistAlerts] dropping hit after {attempts} attempts: {message[:120]}"
            )
            dropped += 1
    return {"sent": sent, "requeued": requeued, "dropped": dropped}
