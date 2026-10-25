"""Realtime evidence-delta publisher and post-store seam wiring."""

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import settings
from app.services import honeypot_srv, realtime_srv

CREDENTIAL_ID = "12345678-aaaa-bbbb-cccc-dddddddddddd"
MESSAGE_ID = "87654321-ffff-eeee-dddd-cccccccccccc"


def _ok_response():
    return SimpleNamespace(raise_for_status=lambda: None)


def test_publish_posts_identifier_only_message_with_service_key(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return _ok_response()

    monkeypatch.setattr(realtime_srv.httpx, "post", fake_post)

    realtime_srv.publish_evidence_delta(CREDENTIAL_ID, MESSAGE_ID)

    assert captured["url"] == (
        "https://example.supabase.co/realtime/v1/api/broadcast"
    )
    assert captured["json"] == {
        "messages": [
            {
                "topic": "evidence",
                "event": "insert",
                "payload": {
                    "credential_id": CREDENTIAL_ID,
                    "message_id": MESSAGE_ID,
                },
                "private": True,
            }
        ]
    }
    assert "content" not in json.dumps(captured["json"])
    assert captured["headers"]["apikey"] == settings.SUPABASE_SERVICE_ROLE_KEY
    assert captured["headers"]["Authorization"] == (
        f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}"
    )


def test_publish_swallows_transport_and_http_failures(monkeypatch, caplog):
    def transport_failure(*_args, **_kwargs):
        raise RuntimeError("realtime unavailable")

    monkeypatch.setattr(realtime_srv.httpx, "post", transport_failure)
    realtime_srv.publish_evidence_delta(CREDENTIAL_ID, MESSAGE_ID)

    def http_failure(*_args, **_kwargs):
        def raise_for_status():
            raise RuntimeError("500 server error")

        return SimpleNamespace(raise_for_status=raise_for_status)

    monkeypatch.setattr(realtime_srv.httpx, "post", http_failure)
    with caplog.at_level("DEBUG", logger="app.services.realtime_srv"):
        realtime_srv.publish_evidence_delta(CREDENTIAL_ID, MESSAGE_ID)

    assert any("evidence delta publish skipped" in record.message for record in caplog.records)


def test_publish_skips_missing_identifiers(monkeypatch):
    post = Mock()
    monkeypatch.setattr(realtime_srv.httpx, "post", post)

    realtime_srv.publish_evidence_delta("", MESSAGE_ID)
    realtime_srv.publish_evidence_delta(CREDENTIAL_ID, "")

    post.assert_not_called()


@pytest.mark.asyncio
async def test_honeypot_post_store_seam_publishes_delta(monkeypatch):
    publish = Mock()
    monkeypatch.setattr(honeypot_srv, "publish_evidence_delta", publish)
    monkeypatch.setattr(
        honeypot_srv,
        "execute_async",
        AsyncMock(return_value=SimpleNamespace(data=[{"id": MESSAGE_ID}])),
    )
    monkeypatch.setattr(settings, "HONEYPOT_FORWARD_RAW_MESSAGES", True)
    monkeypatch.setattr(settings, "HONEYPOT_BROADCAST_TO_TOPIC", False)
    monkeypatch.setattr(
        "app.workers.tasks.flows.heavy.enqueue_enrichment_for_message",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.embedding_hook.embed_message_best_effort",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.watchlist_alerts.alert_on_matches",
        AsyncMock(return_value=0),
    )

    payload = {
        "update_id": 4242,
        "message": {
            "message_id": 7,
            "from": {"id": 1, "username": "sender"},
            "chat": {"id": -1001, "type": "group"},
            "text": "captured content",
        },
    }

    await honeypot_srv.forward_honeypot_message(payload, CREDENTIAL_ID)

    publish.assert_called_once_with(CREDENTIAL_ID, MESSAGE_ID)


@pytest.mark.asyncio
async def test_scrape_realtime_seam_publishes_each_stored_row(monkeypatch):
    from app.workers.tasks.flows import scrape as scrape_flow

    publish = Mock()
    monkeypatch.setattr(
        "app.services.realtime_srv.publish_evidence_delta",
        publish,
    )

    await scrape_flow._publish_realtime_deltas(
        [
            {"id": MESSAGE_ID, "credential_id": CREDENTIAL_ID},
            {"id": None, "credential_id": CREDENTIAL_ID},
        ]
    )

    publish.assert_called_once_with(CREDENTIAL_ID, MESSAGE_ID)

    monkeypatch.setattr(
        "app.services.realtime_srv.publish_evidence_delta",
        Mock(side_effect=RuntimeError("realtime down")),
    )
    await scrape_flow._publish_realtime_deltas(
        [{"id": MESSAGE_ID, "credential_id": CREDENTIAL_ID}]
    )


def test_scrape_store_paths_invoke_realtime_seam():
    from app.workers.tasks.flows import scrape as scrape_flow

    source = inspect.getsource(scrape_flow._exfiltrate_logic)

    assert "await _publish_realtime_deltas(heavy_candidates)" in source
    assert "await _publish_realtime_deltas(preflight_result.data or [])" in source
