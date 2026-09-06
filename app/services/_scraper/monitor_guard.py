"""
Monitor group guard — extracted from scraper_srv.py.

Public API (all re-exported from scraper_srv.py):
    _MONITOR_GROUP_IDS           module-level cache set
    _MONITOR_GROUP_IDS_RESOLVED  cache sentinel
    _resolve_monitor_group_ids_sync()
    _resolve_monitor_group_ids_async()
    _get_monitor_group_ids()
    _is_monitor_group(chat_id)

Canonical storage of the two globals lives HERE.
scraper_srv.py imports and re-exports them so all existing callers are unaffected.
"""
import asyncio
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("scraper.monitor_guard")

# Resolve MONITOR_GROUP_ID to both its username and numeric forms so comparisons
# work regardless of which format is stored (e.g. "@theprawnhunter" vs -1003588166404).
# Populated once at module import via a thread (safe for sync callers) and cached.
_MONITOR_GROUP_IDS: set[str] = set()
_MONITOR_GROUP_IDS_RESOLVED = False


def _resolve_monitor_group_ids_sync() -> set[str]:
    """Resolve MONITOR_GROUP_ID synchronously (safe to call from threads)."""
    global _MONITOR_GROUP_IDS, _MONITOR_GROUP_IDS_RESOLVED
    if _MONITOR_GROUP_IDS_RESOLVED:
        return _MONITOR_GROUP_IDS
    raw = str(settings.MONITOR_GROUP_ID).strip()
    ids: set[str] = {raw}
    # If it's a numeric ID already, also add the supergroup form with -100 prefix if needed
    if raw.lstrip("-").isdigit():
        # Some callers may pass just the bare ID without -100 prefix; store both forms
        n = int(raw)
        ids.add(str(n))
        if n > 0:
            ids.add(str(-n))  # Telegram supergroups may have both forms
    else:
        # Username form -- resolve numeric via Bot API (sync httpx, runs in thread)
        try:
            token = str(settings.MONITOR_BOT_TOKEN).split(",")[0].strip()
            r = httpx.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": raw}, timeout=10
            )
            numeric = r.json().get("result", {}).get("id")
            if numeric:
                ids.add(str(numeric))
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
    _MONITOR_GROUP_IDS = ids
    _MONITOR_GROUP_IDS_RESOLVED = True
    return _MONITOR_GROUP_IDS


async def _resolve_monitor_group_ids_async() -> set[str]:
    """Async-safe wrapper -- runs the sync resolver in a thread so it never blocks the loop."""
    global _MONITOR_GROUP_IDS_RESOLVED
    if _MONITOR_GROUP_IDS_RESOLVED:
        return _MONITOR_GROUP_IDS
    return await asyncio.to_thread(_resolve_monitor_group_ids_sync)


def _get_monitor_group_ids() -> set[str]:
    """Returns cached set of monitor group ID forms.

    Sync callers (guards in scraper init): safe to call directly -- the sync
    resolver uses a blocking httpx.get but that is intentional (called once,
    cached thereafter). In async contexts prefer _resolve_monitor_group_ids_async().
    """
    return _resolve_monitor_group_ids_sync()


def _is_monitor_group(chat_id) -> bool:
    """True if chat_id (any form) refers to our monitor/hub group.

    SAFE to call from sync contexts only. From async contexts, use:
        monitor_ids = await _resolve_monitor_group_ids_async()
        if str(chat_id) in monitor_ids: ...

    Raises RuntimeError if called from inside a running event loop (cold-cache
    case would block the loop via httpx.get). Warm-cache calls (after first
    resolution) return immediately and are safe from any context.
    """
    global _MONITOR_GROUP_IDS_RESOLVED
    if not _MONITOR_GROUP_IDS_RESOLVED:
        # Cold cache -- the sync resolver will make an httpx.get call.
        # If we're inside an async event loop this would block it.
        try:
            asyncio.get_running_loop()
            # There IS a running loop -- refuse to block it.
            raise RuntimeError(
                "_is_monitor_group() called with cold cache from async context. "
                "Use `await _resolve_monitor_group_ids_async()` instead."
            )
        except RuntimeError as exc:
            if "_is_monitor_group" in str(exc):
                raise
            # RuntimeError from get_running_loop means no loop -- safe to proceed sync
    return str(chat_id) in _get_monitor_group_ids()
