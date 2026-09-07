import asyncio
import contextlib
import logging
import os
import urllib.parse

from telethon import TelegramClient
from telethon.sessions import MemorySession

from app.core.config import settings
from app.services._scraper.lifecycle import TelegramClientLifecycle

logger = logging.getLogger("bot_manager")


def _parse_proxy_url(proxy_url: str) -> tuple:
    """Parse a proxy URL into the 6-element tuple expected by Telethon/PySocks."""
    import socks

    parsed = urllib.parse.urlparse(proxy_url)
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"Invalid proxy URL configured: missing hostname or port in '{proxy_url}'")
    if parsed.scheme in ("socks5", "socks5h"):
        proxy_type = socks.SOCKS5
    elif parsed.scheme in ("http", "https"):
        proxy_type = socks.HTTP
    else:
        raise ValueError(f"Unsupported proxy scheme '{parsed.scheme}'. Must be socks5, http, or https.")
    return (proxy_type, parsed.hostname, parsed.port, True, parsed.username, parsed.password)


_MAX_CACHED_CLIENTS = 50  # evict LRU entries beyond this to bound memory
_CLIENT_START_TIMEOUT_SECONDS = 30.0


class BotClientManager:
    """
    Manages a pool of active Telethon (MTProto) clients keyed by bot_token.

    PERF-003 (Telethon connection pooling):
        Each bot_token maps to exactly one long-lived TelegramClient. Repeat
        calls to ``get_client(token)`` return the cached instance instead of
        re-authenticating, cutting per-download setup cost from ~1–3 s (login
        + DC handshake) down to a dict lookup.

        The cache is bounded to ``_MAX_CACHED_CLIENTS`` entries — oldest
        disconnected and evicted on overflow (FIFO by insertion order).
        Callers (broadcaster media download, scraper) never disconnect
        clients directly; ``disconnect_all()`` runs at worker shutdown.
    """
    def __init__(self):
        self.api_id = settings.TELEGRAM_API_ID
        self.api_hash = settings.TELEGRAM_API_HASH
        self._clients: dict = {}   # bot_token -> TelegramClient (insertion-ordered for LRU)
        self._lock = asyncio.Lock()

    async def get_client(self, bot_token: str) -> TelegramClient:
        """
        Returns a connected and authorized Telethon client for the given bot_token.
        Reuses existing connections if available.
        """
        async with self._lock:
            client = self._clients.get(bot_token)

            # Check if client exists and is still connected
            if client:
                if client.is_connected():
                    return client
                else:
                    logger.warning("Existing client for bot disconnected. Reconnecting...")
                    with contextlib.suppress(Exception):
                        await client.disconnect()
                    del self._clients[bot_token]

            # Create new client
            pid = os.getpid()
            logger.info(f"🚀 [BotManager] [PID:{pid}] Creating fresh connection for bot...")
            proxy_tuple = _parse_proxy_url(settings.TELETHON_PROXY_URL) if settings.TELETHON_PROXY_URL else None
            client = TelegramClient(MemorySession(), self.api_id, self.api_hash, proxy=proxy_tuple)
            started = False
            try:
                await asyncio.wait_for(
                    client.start(bot_token=bot_token),
                    timeout=_CLIENT_START_TIMEOUT_SECONDS,
                )
                started = True
            except TimeoutError:
                logger.error(
                    f"[BotManager] Timeout connecting bot client after {_CLIENT_START_TIMEOUT_SECONDS:g}s"
                )
                raise
            finally:
                if not started:
                    await TelegramClientLifecycle(
                        disconnect=client.disconnect,
                        disconnect_timeout=settings.TELEGRAM_CLIENT_DISCONNECT_TIMEOUT_SECONDS,
                        label="bot_manager.start",
                        logger=logger,
                    ).disconnect_safely()

            # Evict oldest entry when cache is full
            if len(self._clients) >= _MAX_CACHED_CLIENTS:
                oldest_token, oldest_client = next(iter(self._clients.items()))
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(oldest_client.disconnect(), timeout=5.0)
                del self._clients[oldest_token]
                logger.info(f"[BotManager] Evicted oldest cached client (cache full at {_MAX_CACHED_CLIENTS})")

            self._clients[bot_token] = client
            return client

    async def disconnect_all(self):
        """Cleanly disconnect all managed clients."""
        async with self._lock:
            for _token, client in self._clients.items():
                logger.info("🔌 [BotManager] Disconnecting bot client...")
                await client.disconnect()
            self._clients.clear()

bot_manager = BotClientManager()
