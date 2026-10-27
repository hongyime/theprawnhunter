"""Periodic refresh of the singleton ``monitor_stats`` row.

Historically two per-row triggers kept this row current, which meant one HOT
update (plus a dead tuple and WAL record) for every message insert/update/
delete. That burned through the free-tier Disk IO budget. The row is now
recomputed once a minute from cheap index-only counts instead.
"""

from __future__ import annotations

from .common import *


@app.task(
    name="stats.refresh_monitor_stats",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def refresh_monitor_stats():
    return _run_sync(_refresh_monitor_stats_async())


async def _refresh_monitor_stats_async():
    from app.infrastructure.database import execute_async

    await execute_async(db.rpc("refresh_monitor_stats"))
    return "monitor_stats refreshed"
