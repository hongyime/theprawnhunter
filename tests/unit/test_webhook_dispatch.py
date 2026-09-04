"""Unit tests for app.core.webhook.dispatch_alert."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.webhook import dispatch_alert


@pytest.mark.asyncio
async def test_dispatch_alert_noop_when_url_empty():
    """dispatch_alert returns immediately when ALERT_WEBHOOK_URL is empty."""
    with patch("app.core.webhook.settings") as mock_settings:
        mock_settings.ALERT_WEBHOOK_URL = ""
        mock_settings.ALERT_WEBHOOK_SECRET = ""
        with patch("app.core.webhook.httpx.AsyncClient") as mock_client:
            assert await dispatch_alert({"event": "test"}) is False
            mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_event_alert_is_default_off():
    with patch("app.core.webhook.settings") as mock_settings:
        mock_settings.ALERT_WEBHOOK_URL = "https://example.com/hook"
        mock_settings.ENABLE_LEGACY_EVENT_ALERTS = False
        with patch("app.core.webhook.httpx.AsyncClient") as mock_client:
            assert await dispatch_alert({"event": "credential_activated"}) is False
            mock_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_routed,legacy_enabled", [(False, True), (True, False)])
async def test_dispatch_alert_posts_only_for_enabled_or_policy_routes(
    policy_routed, legacy_enabled
):
    with patch("app.core.webhook.settings") as mock_settings:
        mock_settings.ALERT_WEBHOOK_URL = "https://example.com/hook"
        mock_settings.ALERT_WEBHOOK_SECRET = ""
        mock_settings.ENABLE_LEGACY_EVENT_ALERTS = legacy_enabled
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=mock_response)
        mock_client_instance = AsyncMock()
        mock_client_instance.post = mock_post
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        with patch("app.core.webhook.httpx.AsyncClient", return_value=mock_client_instance):
            assert await dispatch_alert(
                {"event": "credential_activated", "credential_id": "abc"},
                policy_routed=policy_routed,
            ) is True
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert call_kwargs[1]["json"]["event"] == "credential_activated"


@pytest.mark.asyncio
async def test_dispatch_alert_swallows_errors():
    """dispatch_alert does not raise on HTTP errors."""
    with patch("app.core.webhook.settings") as mock_settings:
        mock_settings.ALERT_WEBHOOK_URL = "https://example.com/hook"
        mock_settings.ALERT_WEBHOOK_SECRET = ""
        mock_settings.ENABLE_LEGACY_EVENT_ALERTS = True
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        with patch("app.core.webhook.httpx.AsyncClient", return_value=mock_client_instance):
            result = await dispatch_alert({"event": "test"})
            assert result is False  # no exception raised
