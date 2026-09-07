"""Shared HTTP client helpers.

Two exports:
- `get_async_http_client(timeout=..., use_proxy=True, ...)` — returns a
  configured `httpx.AsyncClient` with optional proxy support.
- `retry_with_backoff(func, ...)` — exponential-backoff wrapper for async
  callables. Moved here from `app/services/scanners.py` in the 2026-09-06
  DEAD-007 cleanup so it's discoverable and reusable outside the scanner
  layer.
"""
import asyncio
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("http_client")


def get_async_http_client(
    timeout: float | httpx.Timeout = 15.0,
    use_proxy: bool = True,
    **kwargs,
) -> httpx.AsyncClient:
    """Return a configured `httpx.AsyncClient` with optional proxy support."""
    proxy = settings.HTTP_PROXY_URL if (use_proxy and settings.HTTP_PROXY_URL) else None
    follow_redirects = kwargs.pop("follow_redirects", True)
    return httpx.AsyncClient(
        timeout=timeout,
        proxy=proxy,
        follow_redirects=follow_redirects,
        **kwargs,
    )


async def retry_with_backoff(func, max_retries: int = 3, initial_delay: int = 2, backoff_factor: int = 2):
    """Exponential-backoff wrapper for async functions.

    Retries on:
    - HTTP 429 (uses Retry-After header when present)
    - HTTP 500/502/503/504
    - network errors (`httpx.RequestError`, `asyncio.TimeoutError`)

    Fails immediately on 4xx other than 429. Returns `None` after exhausting
    retries.
    """
    retries = 0
    delay = initial_delay
    while retries <= max_retries:
        try:
            return await func()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                retry_after = e.response.headers.get("Retry-After")
                wait_time = int(retry_after) if retry_after and retry_after.isdigit() else delay
                logger.warning(f"⚠️ Rate limited. Waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
            elif e.response.status_code in (500, 502, 503, 504):
                logger.warning(
                    f"⚠️ Server error {e.response.status_code}. Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
            else:
                raise  # 4xx other than 429 — fail fast
        except (TimeoutError, httpx.RequestError) as e:
            logger.warning(f"⚠️ Network error: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

        retries += 1
        delay *= backoff_factor
    return None
