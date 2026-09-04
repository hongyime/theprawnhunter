"""Centralized authentication for /monitor, /health/*, /scan, /ingest, /media endpoints.

Addresses:
- AUDIT-2: constant-time comparison to prevent timing side-channels
- AUDIT-3: single source of truth for the header check, so no future
  endpoint accidentally forgets to gate itself.

Usage in a router:
    from app.core.auth import require_monitor_key
    @router.get("/foo", dependencies=[Depends(require_monitor_key)])
    async def foo(): ...

The dependency raises HTTPException — no need for the endpoint to
touch the header at all.
"""
from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

from fastapi import Header, HTTPException, status

from app.core.config import settings


def require_monitor_key(x_monitor_key: str | None = Header(default=None)) -> UUID:
    """FastAPI dependency: reject if X-Monitor-Key header is missing or wrong.

    - 503 if MONITOR_API_KEY is unset on the server (fail-closed).
    - 403 if the header is missing or does not match.
    - Uses ``hmac.compare_digest`` for constant-time comparison; naive
      ``==`` leaks the position of the first differing byte via response
      timing, which is enough to brute-force short keys.
    """
    expected = settings.MONITOR_API_KEY or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Monitor API key not configured on server",
        )

    provided = x_monitor_key or ""
    # compare_digest requires equal-length inputs to run constant-time on
    # the comparison itself, but the length check leaks nothing useful
    # because the expected length is fixed on the server side.
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing monitor API key",
        )

    # A stable, non-reversible actor ID lets mutating monitor endpoints create
    # useful audit records without persisting or returning the API key itself.
    digest = hashlib.sha256(f"theprawnhunter:monitor-api:v1:{provided}".encode()).digest()
    return UUID(bytes=digest[:16], version=4)
