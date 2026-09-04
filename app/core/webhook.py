"""
Outbound alert webhook dispatcher.
POSTs structured JSON payloads to ALERT_WEBHOOK_URL.
Fire-and-forget: failures are logged at WARNING level and never raised.
"""
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("webhook")


async def dispatch_alert(payload: dict, *, policy_routed: bool = False) -> bool:
    """
    POST payload as JSON to ALERT_WEBHOOK_URL.
    Legacy per-event alerts are default-off. Policy-routed finding alerts are
    allowed whenever ALERT_WEBHOOK_URL is configured.
    All exceptions are caught and logged — never propagates.
    """
    url = settings.ALERT_WEBHOOK_URL
    if not url:
        return False
    if not policy_routed and not settings.ENABLE_LEGACY_EVENT_ALERTS:
        logger.debug("[Webhook] Legacy event alert suppressed by default-off gate")
        return False

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
            return True
    except Exception as exc:
        logger.warning(f"[Webhook] Dispatch to {host} failed: {exc}")
        return False
