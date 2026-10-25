"""Best-effort realtime delta publisher for the dashboard evidence feed.

The backend stores captured messages with the Supabase service key. Once a row
is committed, a tiny identifier-only delta is broadcast on the private
``evidence`` channel so subscribed dashboard operators can refetch the newest
page immediately instead of waiting for the fallback poll.

Delivery uses the Supabase Realtime REST broadcast endpoint because the
synchronous ``supabase-py`` client in use (2.28.x) exposes realtime channels
only through its async client (``Client.channel`` raises ``NotImplementedError``
in the sync build). REST keeps this helper synchronous and easy to isolate.

Every failure is logged at debug and swallowed: realtime is an enhancement and
must never break capture. Message content is never logged or published — the
payload carries identifiers only.
"""

from typing import Any

import httpx

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

EVIDENCE_TOPIC = "evidence"
EVIDENCE_EVENT = "insert"
REALTIME_TIMEOUT_SECONDS = 2.0


def _broadcast_url() -> str:
    return f"{settings.SUPABASE_URL.rstrip('/')}/realtime/v1/api/broadcast"


def publish_evidence_delta(credential_id: str, message_id: str) -> None:
    """Broadcast ``{credential_id, message_id}`` on the private evidence topic.

    Best-effort: any failure (configuration, transport, non-2xx) is logged at
    debug and swallowed. Identifiers are stringified; falsy ids are skipped.
    """
    if not credential_id or not message_id:
        return

    payload: dict[str, Any] = {
        "credential_id": str(credential_id),
        "message_id": str(message_id),
    }

    try:
        service_key = settings.SUPABASE_SERVICE_ROLE_KEY
        response = httpx.post(
            _broadcast_url(),
            json={
                "messages": [
                    {
                        "topic": EVIDENCE_TOPIC,
                        "event": EVIDENCE_EVENT,
                        "payload": payload,
                        "private": True,
                    }
                ]
            },
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
            },
            timeout=REALTIME_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - realtime is a best-effort enhancement
        logger.debug(f"[Realtime] evidence delta publish skipped: {exc}")


__all__ = ["EVIDENCE_EVENT", "EVIDENCE_TOPIC", "publish_evidence_delta"]
