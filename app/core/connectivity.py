"""
Internet connectivity check and wait utilities.

Used by workers and bot_listener to:
1. Wait for internet on startup (machine boot, container restart)
2. Gate task execution when connectivity is lost
3. Suppress log spam during extended outages
"""

import asyncio
import logging
import socket
import time

logger = logging.getLogger("connectivity")

# Track state to avoid log spam during extended outages
_last_online = True
_offline_logged_at: float = 0


def check_internet(timeout: float = 5.0) -> bool:
    """Quick connectivity check via TCP to Google DNS (8.8.8.8:53)."""
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=timeout)
        return True
    except OSError:
        return False


def wait_for_internet_sync(max_wait: int = 600, check_interval: int = 10) -> bool:
    """
    Blocks until internet is available. Returns True when connected,
    False if max_wait exceeded. Logs once per minute to avoid spam.
    """
    global _last_online, _offline_logged_at

    if check_internet():
        if not _last_online:
            logger.info("Internet connectivity restored.")
            _last_online = True
        return True

    now = time.time()
    if now - _offline_logged_at > 60:
        logger.warning(
            f"No internet connectivity. Waiting up to {max_wait}s for connection..."
        )
        _offline_logged_at = now
    _last_online = False

    elapsed = 0
    while elapsed < max_wait:
        time.sleep(check_interval)
        elapsed += check_interval
        if check_internet():
            logger.info(f"Internet connectivity restored after ~{elapsed}s.")
            _last_online = True
            return True
        if elapsed % 60 == 0:
            logger.info(f"Still waiting for internet... ({elapsed}s elapsed)")

    logger.error(f"Internet connectivity not restored after {max_wait}s.")
    return False


async def wait_for_internet_async(max_wait: int = 600, check_interval: int = 10) -> bool:
    """Async version of wait_for_internet_sync."""
    global _last_online, _offline_logged_at

    if check_internet():
        if not _last_online:
            logger.info("Internet connectivity restored.")
            _last_online = True
        return True

    now = time.time()
    if now - _offline_logged_at > 60:
        logger.warning(
            f"No internet connectivity. Waiting up to {max_wait}s for connection..."
        )
        _offline_logged_at = now
    _last_online = False

    elapsed = 0
    while elapsed < max_wait:
        await asyncio.sleep(check_interval)
        elapsed += check_interval
        if check_internet():
            logger.info(f"Internet connectivity restored after ~{elapsed}s.")
            _last_online = True
            return True
        if elapsed % 60 == 0:
            logger.info(f"Still waiting for internet... ({elapsed}s elapsed)")

    logger.error(f"Internet connectivity not restored after {max_wait}s.")
    return False
