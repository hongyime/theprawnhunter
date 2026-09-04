"""Privacy-minimized funnel tracking for bots owned by this deployment only."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.database import db

ENGAGEMENT_EVENT_TYPES = frozenset(
    {"start", "first_inbound", "qualified", "handoff", "outcome", "opt_out", "block_report"}
)
_PAYLOAD_PATTERN = re.compile(
    r"^campaign_(?P<campaign>[a-z0-9][a-z0-9_-]{0,63})"
    r"__source_(?P<source>[a-z0-9][a-z0-9_-]{0,63})$",
    re.IGNORECASE,
)
_ALLOWED_METADATA = frozenset(
    {"entry", "payload_valid", "qualification_code", "outcome_code", "reason_code"}
)
_CONTEXT_KEY = "owned_bot_engagement_attribution"
_EXCLUDED_CONTEXT_KEY = "owned_bot_engagement_excluded"


@dataclass(frozen=True)
class CampaignAttribution:
    campaign_id: str
    campaign_source: str
    payload_valid: bool


def parse_campaign_payload(
    payload: str | None, *, excluded_payload: str | None = None
) -> CampaignAttribution | None:
    """Parse transparent deep-link attribution; exclude captured-redirect traffic."""
    candidate = (payload or "").strip()
    if excluded_payload and candidate == excluded_payload.strip():
        return None
    if not candidate:
        return CampaignAttribution("organic", "direct", True)
    match = _PAYLOAD_PATTERN.fullmatch(candidate)
    if not match:
        return CampaignAttribution("unattributed", "deep_link", False)
    return CampaignAttribution(
        match.group("campaign").lower(), match.group("source").lower(), True
    )


def pseudonymize_engagement_subject(subject_id: Any, secret: str | None = None) -> str:
    secret_value = secret or settings.PSEUDONYMIZATION_KEY or settings.ENCRYPTION_KEY
    if not secret_value:
        raise ValueError("PSEUDONYMIZATION_KEY or ENCRYPTION_KEY is required")
    material = f"theprawnhunter:owned-bot-engagement:v1:{subject_id}".encode()
    return hmac.new(secret_value.encode(), material, hashlib.sha256).hexdigest()[:24]


def _event_key(
    owned_bot_id: int,
    subject_pseudonym: str,
    attribution: CampaignAttribution,
    event_type: str,
) -> str:
    material = ":".join(
        (
            "owned-bot-event-v1",
            str(owned_bot_id),
            subject_pseudonym,
            attribution.campaign_id,
            attribution.campaign_source,
            event_type,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _redact_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (metadata or {}).items()
        if key in _ALLOWED_METADATA and isinstance(value, (str, bool, int, float))
    }


async def record_engagement_event(
    *,
    owned_bot_id: int,
    subject_id: Any,
    event_type: str,
    attribution: CampaignAttribution,
    metadata: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    client: Any = db,
) -> dict[str, Any]:
    if event_type not in ENGAGEMENT_EVENT_TYPES:
        raise ValueError(f"unsupported engagement event: {event_type}")
    subject_pseudonym = pseudonymize_engagement_subject(subject_id)
    occurred = (occurred_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    params = {
        "p_event_key": _event_key(
            owned_bot_id, subject_pseudonym, attribution, event_type
        ),
        "p_owned_bot_id": int(owned_bot_id),
        "p_subject_pseudonym": subject_pseudonym,
        "p_campaign_id": attribution.campaign_id,
        "p_campaign_source": attribution.campaign_source,
        "p_event_type": event_type,
        "p_occurred_at": occurred,
        "p_metadata_redacted": _redact_metadata(metadata),
    }
    response = await asyncio.to_thread(
        client.rpc("upsert_engagement_event", params).execute
    )
    return {
        "status": "recorded",
        "event_type": event_type,
        "subject_pseudonym": subject_pseudonym,
        "event_id": response.data,
    }


def _attribution_from_context(context: Any) -> CampaignAttribution | None:
    if context.user_data.get(_EXCLUDED_CONTEXT_KEY):
        return None
    stored = context.user_data.get(_CONTEXT_KEY, {})
    return CampaignAttribution(
        str(stored.get("campaign_id") or "organic"),
        str(stored.get("campaign_source") or "direct"),
        bool(stored.get("payload_valid", True)),
    )


async def track_owned_bot_start(update: Any, context: Any) -> dict[str, Any]:
    """Track an explicit /start handled by one of this process's monitor bots."""
    payload = context.args[0] if getattr(context, "args", None) else None
    attribution = parse_campaign_payload(
        payload, excluded_payload=settings.HONEYPOT_REDIRECT_DEEPLINK
    )
    if attribution is None:
        context.user_data.pop(_CONTEXT_KEY, None)
        context.user_data[_EXCLUDED_CONTEXT_KEY] = True
        return {"status": "excluded", "reason": "captured_redirect_source"}
    context.user_data.pop(_EXCLUDED_CONTEXT_KEY, None)
    context.user_data[_CONTEXT_KEY] = {
        "campaign_id": attribution.campaign_id,
        "campaign_source": attribution.campaign_source,
        "payload_valid": attribution.payload_valid,
    }
    context.user_data.pop("owned_bot_first_inbound_tracked", None)
    return await record_engagement_event(
        owned_bot_id=int(context.bot.id),
        subject_id=update.effective_user.id,
        event_type="start",
        attribution=attribution,
        metadata={"entry": "telegram_start", "payload_valid": attribution.payload_valid},
    )


async def track_owned_bot_first_inbound(update: Any, context: Any) -> dict[str, Any]:
    """Track the first private non-command message; content is never persisted."""
    attribution = _attribution_from_context(context)
    if attribution is None:
        return {"status": "excluded", "reason": "captured_redirect_source"}
    return await record_engagement_event(
        owned_bot_id=int(context.bot.id),
        subject_id=update.effective_user.id,
        event_type="first_inbound",
        attribution=attribution,
        metadata={"entry": "private_text"},
    )


async def track_owned_bot_opt_out(update: Any, context: Any) -> dict[str, Any]:
    attribution = _attribution_from_context(context)
    if attribution is None:
        return {"status": "excluded", "reason": "captured_redirect_source"}
    result = await record_engagement_event(
        owned_bot_id=int(context.bot.id),
        subject_id=update.effective_user.id,
        event_type="opt_out",
        attribution=attribution,
        metadata={"reason_code": "user_command"},
    )
    context.user_data.pop(_CONTEXT_KEY, None)
    context.user_data.pop(_EXCLUDED_CONTEXT_KEY, None)
    context.user_data.pop("owned_bot_first_inbound_tracked", None)
    return result
