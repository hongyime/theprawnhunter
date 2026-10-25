"""Alert dispatch for watchlist hits on freshly stored messages.

The capture lane stores a message, then calls :func:`alert_on_matches`. This
module evaluates the content through :func:`app.services.watchlists.evaluate`
(which records hits and enforces duplicate suppression and cooldowns) and
sends one compact Telegram alert per emitted hit to the Watchlist Hits topic
(with General as the fallback).

Contract:
* Best-effort only. Every failure path logs and returns ``0``; capture must
  never break because alerting failed.
* Never log message content, matched text, or tokens.
* Matching and delivery are reused from ``watchlists`` and
  ``BroadcasterService.send_watchlist_alert`` — nothing is reimplemented here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.config import settings
from app.core.database import db
from app.core.logger import get_logger
from app.infrastructure.database import execute_async
from app.services import watchlists

logger = get_logger(__name__)

SNIPPET_LIMIT = 300
CREDENTIAL_PREFIX_LENGTH = 8
DEEPLINK_PREFIX = "📎 Open in Telegram: "
DEEPLINK_TEMPLATE = "https://t.me/c/{internal_group_id}/{thread_id}/{msg_id}"
TOPIC_DEEPLINK_TEMPLATE = "https://t.me/c/{internal_group_id}/{thread_id}"
GROUP_ID_CACHE_PREFIX = "group:numeric_id:"
GROUP_ID_CACHE_TTL = 7 * 24 * 60 * 60


def _short_credential(credential_id: Any) -> str:
    """Return a short, log-safe credential reference."""
    if not credential_id:
        return "global"
    value = str(credential_id)
    return value[:CREDENTIAL_PREFIX_LENGTH] or value


def _internal_group_id(group_id: Any) -> str | None:
    """Return the ``t.me/c`` form of a supergroup id (strip ``-100``/``-``)."""
    if group_id is None:
        return None
    value = str(group_id).strip()
    if not value:
        return None
    if value.startswith("-100"):
        value = value[4:]
    elif value.startswith("-"):
        value = value[1:]
    if not value.isdigit():
        return None
    return value


def build_message_deeplink(
    group_id: Any,
    thread_id: Any,
    msg_id: Any,
) -> str | None:
    """Build a Telegram deep link to a message inside a forum topic.

    Pure and side-effect free. Returns ``None`` when any component is missing
    or not a positive integer, so callers can silently omit the line.
    """
    internal_group_id = _internal_group_id(group_id)
    if internal_group_id is None:
        return None
    try:
        thread = int(thread_id)
    except (TypeError, ValueError):
        return None
    if thread <= 0:
        return None
    try:
        message = int(msg_id)
    except (TypeError, ValueError):
        message = 0
    if message > 0:
        return DEEPLINK_TEMPLATE.format(
            internal_group_id=internal_group_id,
            thread_id=thread,
            msg_id=message,
        )
    return TOPIC_DEEPLINK_TEMPLATE.format(
        internal_group_id=internal_group_id,
        thread_id=thread,
    )


def _group_id_cache_key(group_id: Any) -> str:
    """Return the Redis key holding the numeric id for a group reference."""
    return f"{GROUP_ID_CACHE_PREFIX}{group_id}"


async def _resolve_numeric_group_id(broadcaster: Any, group_id: Any) -> int | None:
    """Resolve a username/`@handle` group reference to its numeric chat id.

    Cached in Redis for :data:`GROUP_ID_CACHE_TTL` so this costs one Bot API
    call per process lifetime, not one per alert. Never raises; returns
    ``None`` when the lookup or cache is unavailable.
    """
    cache_key = _group_id_cache_key(group_id)
    client = None
    try:
        import redis

        client = redis.from_url(
            getattr(settings, "REDIS_URL", ""), decode_responses=True
        )
        cached = client.get(cache_key)
        if cached is not None:
            try:
                return int(cached)
            except (TypeError, ValueError):
                logger.debug(
                    f"[WatchlistAlert] invalid cached group id for {group_id}"
                )
    except Exception as exc:  # noqa: BLE001 - cache is optional
        logger.debug(f"[WatchlistAlert] group id cache read failed: {exc}")

    try:
        bot = broadcaster._get_bot_instance(broadcaster.bot_tokens[0])
        chat = await bot.get_chat(group_id)
        numeric_id = getattr(chat, "id", None)
        if numeric_id is None:
            return None
        numeric_id = int(numeric_id)
    except Exception as exc:  # noqa: BLE001 - link is optional
        logger.warning(
            f"[WatchlistAlert] could not resolve numeric id for group {group_id}: {exc}"
        )
        return None

    if client is not None:
        try:
            client.setex(cache_key, GROUP_ID_CACHE_TTL, str(numeric_id))
        except Exception as exc:  # noqa: BLE001 - cache is optional
            logger.debug(f"[WatchlistAlert] group id cache write failed: {exc}")

    return numeric_id


async def _resolve_message_deeplink(
    *,
    message_id: Any,
    credential_id: Any,
) -> str | None:
    """Best-effort deep link for a stored message; never raises.

    Lookup-only: an unknown topic yields no link rather than creating an empty
    one. The message component is the broadcast copy's id, never the source
    chat's id (which would point at an unrelated message in our group).
    """
    group_id = getattr(settings, "MONITOR_GROUP_ID", None)
    if group_id is None or not credential_id or message_id is None:
        return None

    try:
        from app.services.broadcaster_srv import _username_from_meta
        from app.workers.tasks.flow_tasks import get_broadcaster

        broadcaster = get_broadcaster()
        numeric_group_id = group_id
        if _internal_group_id(group_id) is None:
            numeric_group_id = await _resolve_numeric_group_id(broadcaster, group_id)
            if numeric_group_id is None:
                return None

        credential_res = await execute_async(
            db.table("discovered_credentials")
            .select("meta")
            .eq("id", credential_id)
            .limit(1)
        )
        credential_rows = credential_res.data or []
        meta = credential_rows[0].get("meta") if credential_rows else None
        username = _username_from_meta(meta)
        stored_topic_id = meta.get("topic_id") if isinstance(meta, dict) else None
        try:
            stored_thread_id = int(stored_topic_id) if stored_topic_id else None
        except (TypeError, ValueError):
            stored_thread_id = None
        if not username and not stored_thread_id:
            return None

        message_res = await execute_async(
            db.table("exfiltrated_messages")
            .select("telegram_msg_id,broadcast_message_id")
            .eq("id", message_id)
            .limit(1)
        )
        message_rows = message_res.data or []
        broadcast_msg_id = (
            message_rows[0].get("broadcast_message_id") if message_rows else None
        )

        thread_id = stored_thread_id
        if thread_id is None and username:
            thread_id = await broadcaster.find_existing_topic_id(group_id, username)
        if thread_id is None:
            return None
        return build_message_deeplink(
            numeric_group_id, thread_id, broadcast_msg_id
        )
    except Exception as exc:  # noqa: BLE001 - links must degrade silently
        logger.warning(
            f"[WatchlistAlert] deep link skipped for message {message_id}: {exc}"
        )
        return None


def _build_alert_body(
    match: Mapping[str, Any],
    *,
    message_id: Any,
    credential_id: Any,
    deeplink: str | None = None,
) -> str:
    """Build the compact alert body for a single watchlist hit."""
    name = str(match.get("name") or match.get("rule_id") or "unnamed rule")
    rule_id = str(match.get("rule_id") or "unknown")
    matched_text = str(match.get("matched_text") or "")[:SNIPPET_LIMIT]
    short_cred = _short_credential(match.get("credential_id") or credential_id)
    body = (
        f"Watchlist hit: {name}\n"
        f"Rule: {rule_id}\n"
        f"Credential: {short_cred}\n"
        f"Match: {matched_text}\n"
        f"Jump: credential {short_cred} / message {message_id}"
    )
    if deeplink:
        body = f"{body}\n{DEEPLINK_PREFIX}{deeplink}"
    return body


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

    link_credential_id = credential_id or next(
        (m.get("credential_id") for m in matches if m.get("credential_id")),
        None,
    )
    deeplink = await _resolve_message_deeplink(
        message_id=message_id,
        credential_id=link_credential_id,
    )

    sent = 0
    for match in matches:
        try:
            body = _build_alert_body(
                match,
                message_id=message_id,
                credential_id=credential_id,
                deeplink=deeplink,
            )
            delivered = await get_broadcaster().send_watchlist_alert(body)
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


__all__ = [
    "CREDENTIAL_PREFIX_LENGTH",
    "DEEPLINK_PREFIX",
    "SNIPPET_LIMIT",
    "alert_on_matches",
    "build_message_deeplink",
]
