"""
Outbound alert webhook dispatcher.
POSTs a structured JSON payload to ALERT_WEBHOOK_URL on key pipeline events.
Fire-and-forget: failures are logged at WARNING level and never raised.
"""
import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings

logger = logging.getLogger("webhook")


async def dispatch_alert(payload: dict) -> None:
    """
    POST payload as JSON to ALERT_WEBHOOK_URL.
    No-op when ALERT_WEBHOOK_URL is unset.
    All exceptions are caught and logged — never propagates.
    """
    url = settings.ALERT_WEBHOOK_URL
    if not url:
        return

    headers = {"Content-Type": "application/json"}
    if settings.ALERT_WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = settings.ALERT_WEBHOOK_SECRET

    # Log the host only — never log the full URL (may contain secrets in path)
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
    except Exception:
        host = "<unknown>"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            logger.debug(f"[Webhook] Alert dispatched to {host} → {r.status_code}")
    except Exception as exc:
        logger.warning(f"[Webhook] Dispatch to {host} failed: {exc}")
