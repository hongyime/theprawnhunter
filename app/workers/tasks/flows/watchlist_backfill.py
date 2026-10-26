"""Advance the historical watchlist scan one window per run."""

from .common import *

from app.infrastructure.database import execute_async
from app.workers.celery_app import _run_sync  # noqa: E402

CURSOR_KEY = "watchlist:backfill:cursor"
DEFAULT_LIMIT = 20_000


@app.task(name="flow.backfill_watchlists")
def backfill_watchlists(limit: int = DEFAULT_LIMIT, alert: bool = False):
    return _run_sync(_backfill_watchlists_async(limit, alert))


async def _backfill_watchlists_async(limit: int, alert: bool) -> str:
    try:
        from app.services import watchlists

        cursor = None
        try:
            cursor = redis_client.get(CURSOR_KEY)
        except Exception:  # noqa: BLE001
            cursor = None

        query = (
            db.table("exfiltrated_messages")
            .select("id,credential_id,content,created_at")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if cursor:
            query = query.lt("created_at", cursor)
        response = await execute_async(query)
        rows = response.data or []
        scannable = [row for row in rows if (row.get("content") or "").strip()]
        if not rows:
            logger.info("[WatchlistBackfill] corpus fully scanned")
            return "watchlist_backfill:done"
        if not scannable:
            redis_client.setex(CURSOR_KEY, 30 * 86400, rows[-1]["created_at"])
            return "watchlist_backfill:scanned=0"

        hits = 0
        for row in scannable:
            try:
                matches = await watchlists.evaluate(
                    row["content"],
                    message_id=row["id"],
                    credential_id=row.get("credential_id"),
                )
                hits += len(matches)
                if matches and alert:
                    from app.services.watchlist_alerts import alert_on_matches

                    await alert_on_matches(
                        row["content"],
                        message_id=row["id"],
                        credential_id=row.get("credential_id"),
                    )
            except Exception as error:  # noqa: BLE001 - keep advancing
                logger.debug(f"[WatchlistBackfill] row {row['id']} skipped: {type(error).__name__}")

        redis_client.setex(CURSOR_KEY, 30 * 86400, rows[-1]["created_at"])
        logger.info(
            f"[WatchlistBackfill] scanned={len(scannable)}/{len(rows)} hits={hits} "
            f"cursor={rows[-1]['created_at']}"
        )
        return f"watchlist_backfill:scanned={len(scannable)},hits={hits}"
    except Exception as error:  # noqa: BLE001 - never break the worker
        logger.warning(f"[WatchlistBackfill] aborted: {type(error).__name__}")
        return "watchlist_backfill:error"
