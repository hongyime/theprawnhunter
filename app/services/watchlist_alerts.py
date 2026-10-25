"""Alert dispatch for watchlist hits on freshly stored messages.

The capture lane stores a message, then calls :func:`alert_on_matches`. This
module evaluates the content through :func:`app.services.watchlists.evaluate`
(which records hits and enforces duplicate suppression and cooldowns) and
sends one compact Telegram alert per emitted hit to the control channel.

Contract:
* Best-effort only. Every failure path logs and returns ``0``; capture must
  never break because alerting failed.
* Never log message content, matched text, or tokens.
* Matching and delivery are reused from ``watchlists`` and
  ``BroadcasterService.send_alert`` — nothing is reimplemented here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.logger import get_logger
from app.services import watchlists

logger = get_logger(__name__)

SNIPPET_LIMIT = 300
CREDENTIAL_PREFIX_LENGTH = 8


def _short_credential(credential_id: Any) -> str:
    """Return a short, log-safe credential reference."""
    if not credential_id:
        return "global"
    value = str(credential_id)
    return value[:CREDENTIAL_PREFIX_LENGTH] or value


def _build_alert_body(
    match: Mapping[str, Any],
    *,
    message_id: Any,
    credential_id: Any,
) -> str:
    """Build the compact alert body for a single watchlist hit."""
    name = str(match.get("name") or match.get("rule_id") or "unnamed rule")
    rule_id = str(match.get("rule_id") or "unknown")
    matched_text = str(match.get("matched_text") or "")[:SNIPPET_LIMIT]
    short_cred = _short_credential(match.get("credential_id") or credential_id)
    return (
        f"Watchlist hit: {name}\n"
        f"Rule: {rule_id}\n"
        f"Credential: {short_cred}\n"
        f"Match: {matched_text}\n"
        f"Jump: credential {short_cred} / message {message_id}"
    )


async def alert_on_matches(
    content: str,
    *,
    message_id: Any,
    credential_id: Any = None,
) -> int:
    """Evaluate ``content`` and send one alert per emitted watchlist hit.

    Returns the number of alerts successfully handed to the broadcaster.
    Never raises: evaluation, formatting, and delivery failures are logged and
    result in ``0`` (per-hit delivery failures are skipped).
    """
    try:
        matches = await watchlists.evaluate(
            content,
            message_id=message_id,
            credential_id=credential_id,
        )
    except Exception as exc:  # noqa: BLE001 - alerting must never break capture
        logger.warning(
            f"[WatchlistAlert] evaluation failed for message {message_id}: {exc}"
        )
        return 0

    if not matches:
        return 0

    try:
        from app.workers.tasks.flow_tasks import get_broadcaster
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency
        logger.warning(f"[WatchlistAlert] broadcaster unavailable: {exc}")
        return 0

    sent = 0
    for match in matches:
        try:
            body = _build_alert_body(
                match,
                message_id=message_id,
                credential_id=credential_id,
            )
            delivered = await get_broadcaster().send_alert(body)
        except Exception as exc:  # noqa: BLE001 - one hit must not stop the rest
            logger.warning(
                f"[WatchlistAlert] alert dispatch failed for message {message_id}: {exc}"
            )
            continue
        if delivered is False:
            logger.warning(
                f"[WatchlistAlert] alert not delivered for message {message_id}"
            )
            continue
        sent += 1

    return sent


__all__ = ["CREDENTIAL_PREFIX_LENGTH", "SNIPPET_LIMIT", "alert_on_matches"]
