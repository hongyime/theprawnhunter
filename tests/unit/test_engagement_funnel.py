"""Owned-bot voluntary engagement funnel tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.engagement import (
    CampaignAttribution,
    parse_campaign_payload,
    pseudonymize_engagement_subject,
    record_engagement_event,
    track_owned_bot_first_inbound,
    track_owned_bot_opt_out,
    track_owned_bot_start,
)


def test_transparent_campaign_payload_and_safe_fallbacks():
    assert parse_campaign_payload("campaign_launch__source_website") == CampaignAttribution(
        "launch", "website", True
    )
    assert parse_campaign_payload(None) == CampaignAttribution("organic", "direct", True)
    assert parse_campaign_payload("opaque-secret-value") == CampaignAttribution(
        "unattributed", "deep_link", False
    )


def test_captured_redirect_payload_is_excluded_from_voluntary_funnel():
    assert parse_campaign_payload("migrate", excluded_payload="migrate") is None


def test_engagement_pseudonym_is_keyed_stable_and_domain_separated():
    secret = "a" * 32
    assert pseudonymize_engagement_subject(123, secret) == pseudonymize_engagement_subject(
        123, secret
    )
    assert pseudonymize_engagement_subject(123, secret) != pseudonymize_engagement_subject(
        123, "b" * 32
    )
    assert "123" not in pseudonymize_engagement_subject(123, secret)


class _FakeQuery:
    def __init__(self):
        self.data = "event-id"

    def execute(self):
        return self


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name: str, params: dict):
        self.calls.append((name, params))
        return _FakeQuery()


@pytest.mark.asyncio
async def test_event_rpc_is_idempotent_and_metadata_is_allowlisted():
    client = _FakeClient()
    attribution = CampaignAttribution("launch", "website", True)
    kwargs = {
        "owned_bot_id": 42,
        "subject_id": 987654321,
        "event_type": "qualified",
        "attribution": attribution,
        "metadata": {
            "qualification_code": "supported_intent",
            "private_message": "must-not-leave-process",
        },
        "occurred_at": datetime(2026, 9, 4, tzinfo=UTC),
        "client": client,
    }
    await record_engagement_event(**kwargs)
    await record_engagement_event(**kwargs)

    first = client.calls[0][1]
    second = client.calls[1][1]
    assert first["p_event_key"] == second["p_event_key"]
    assert first["p_metadata_redacted"] == {
        "qualification_code": "supported_intent"
    }
    assert "987654321" not in str(first)


@pytest.mark.asyncio
async def test_owned_start_excludes_known_captured_redirect(monkeypatch):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123))
    context = SimpleNamespace(
        args=["migrate"],
        user_data={},
        bot=SimpleNamespace(id=42),
    )
    monkeypatch.setattr(
        "app.services.engagement.settings.HONEYPOT_REDIRECT_DEEPLINK", "migrate"
    )

    result = await track_owned_bot_start(update, context)

    assert result == {"status": "excluded", "reason": "captured_redirect_source"}
    assert context.user_data == {"owned_bot_engagement_excluded": True}

    assert await track_owned_bot_first_inbound(update, context) == result
    assert await track_owned_bot_opt_out(update, context) == result
