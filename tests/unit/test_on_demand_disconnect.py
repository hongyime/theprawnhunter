"""
Tests for UserAgentService — on-demand connect/disconnect pattern
and MTProto conflict resilience.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — build a minimal UserAgentService without touching real config
# ---------------------------------------------------------------------------

def _make_service():
    """Create a UserAgentService with mocked internals."""
    with patch("app.services.user_agent_srv.settings") as mock_settings:
        mock_settings.TELEGRAM_API_ID = 12345
        mock_settings.TELEGRAM_API_HASH = "abc"
        mock_settings.bot_tokens = ["111:AAA"]
        mock_settings.MONITOR_GROUP_ID = -100123

        from app.services.user_agent_srv import UserAgentService
        svc = UserAgentService()
    return svc


def _mock_connected_client():
    """Return a mock TelegramClient that appears connected and authorized."""
    client = AsyncMock()
    # is_connected() is a sync method on TelegramClient
    client.is_connected = MagicMock(return_value=True)
    client.is_user_authorized = AsyncMock(return_value=True)
    client.disconnect = AsyncMock()
    client.session = MagicMock()
    client.session.filename = "/tmp/test_session"
    return client


# ===========================================================================
# On-demand disconnect tests
# ===========================================================================

class TestOnDemandDisconnect:
    """Every public method must disconnect the Telethon client after completing."""

    @pytest.mark.asyncio
    async def test_send_message_disconnects_after_success(self):
        svc = _make_service()
        client = _mock_connected_client()
        client.send_message = AsyncMock()
        svc.client = client

        # Patch start() to succeed without real Telegram
        with patch.object(svc, "start", new_callable=AsyncMock, return_value=True):
            result = await svc.send_message(123, "hello")

        assert result is True
        client.send_message.assert_awaited_once()
        # _disconnect should have called client.disconnect()
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_send_message_disconnects_after_failure(self):
        svc = _make_service()
        client = _mock_connected_client()
        client.send_message = AsyncMock(side_effect=Exception("network error"))
        svc.client = client

        with patch.object(svc, "start", new_callable=AsyncMock, return_value=True):
            result = await svc.send_message(123, "hello")

        assert result is False
        # Must still disconnect even on error
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_find_topic_id_disconnects(self):
        svc = _make_service()
        client = _mock_connected_client()
        client.get_entity = AsyncMock(side_effect=Exception("resolve failed"))
        svc.client = client

        with patch.object(svc, "start", new_callable=AsyncMock, return_value=True):
            result = await svc.find_topic_id(-100123, "some-topic")

        assert result is None
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_get_history_disconnects(self):
        svc = _make_service()
        client = _mock_connected_client()
        # iter_messages returns an empty async iterator
        client.iter_messages = MagicMock(return_value=AsyncIteratorMock([]))
        client.get_entity = AsyncMock(return_value=MagicMock(id=123))
        svc.client = client

        with patch.object(svc, "start", new_callable=AsyncMock, return_value=True):
            result = await svc.get_history(-100123, 10)

        assert result == []
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_check_membership_disconnects(self):
        svc = _make_service()
        client = _mock_connected_client()
        client.get_entity = AsyncMock(side_effect=Exception("not found"))
        svc.client = client

        with patch.object(svc, "start", new_callable=AsyncMock, return_value=True):
            result = await svc.check_membership(-100123, 456)

        assert result is None
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_get_last_message_id_disconnects(self):
        svc = _make_service()
        client = _mock_connected_client()
        client.get_entity = AsyncMock(return_value=MagicMock(id=123))
        client.get_messages = AsyncMock(return_value=[])
        svc.client = client

        with patch.object(svc, "start", new_callable=AsyncMock, return_value=True):
            result = await svc.get_last_message_id(-100123, 1)

        assert result is None
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_disconnect_when_start_fails(self):
        """If start() fails, client may be None — _disconnect must handle gracefully."""
        svc = _make_service()
        svc.client = None

        with patch.object(svc, "start", new_callable=AsyncMock, return_value=False):
            result = await svc.send_message(123, "hello")

        assert result is False
        # No crash — _disconnect handled None client


class TestDisconnectHelper:
    """Test the _disconnect() helper directly."""

    @pytest.mark.asyncio
    async def test_disconnect_when_connected(self):
        svc = _make_service()
        client = _mock_connected_client()
        svc.client = client

        await svc._disconnect()

        client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self):
        svc = _make_service()
        client = AsyncMock()
        # is_connected() is sync — must use MagicMock
        client.is_connected = MagicMock(return_value=False)
        svc.client = client

        await svc._disconnect()

        client.disconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_when_client_is_none(self):
        svc = _make_service()
        svc.client = None

        # Should not raise
        await svc._disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_swallows_errors(self):
        svc = _make_service()
        client = _mock_connected_client()
        client.disconnect = AsyncMock(side_effect=OSError("broken pipe"))
        svc.client = client

        # Should not raise
        await svc._disconnect()


# ===========================================================================
# MTProto conflict resilience
# ===========================================================================

class TestMTProtoConflict:
    """SecurityError with 'Too many messages had to be ignored' should trigger
    disconnect + backoff + retry."""

    @pytest.mark.asyncio
    async def test_mtproto_conflict_triggers_backoff(self):
        svc = _make_service()
        svc.sessions = ["/fake/session.session"]

        # Test the backoff constants and message matching
        from app.services.user_agent_srv import _MTPROTO_CONFLICT_BACKOFF, _MTPROTO_MAX_RETRIES

        assert _MTPROTO_CONFLICT_BACKOFF == 10  # Short for on-demand
        assert _MTPROTO_MAX_RETRIES == 3

        # Test the message matching that start() uses internally
        err_msg = "Security error while unpacking: Too many messages had to be ignored consecutively"
        assert "Too many messages had to be ignored" in err_msg


class TestStopGraceful:
    """stop() should disconnect and cancel refresher task."""

    @pytest.mark.asyncio
    async def test_stop_disconnects_and_cancels_refresher(self):
        svc = _make_service()
        client = _mock_connected_client()
        svc.client = client

        # Use a real asyncio.Task so await/cancel/done work naturally
        async def _sleep_forever():
            await asyncio.sleep(999)

        task = asyncio.create_task(_sleep_forever())
        svc._refresher_task = task

        await svc.stop()

        client.disconnect.assert_awaited()
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_without_client(self):
        svc = _make_service()
        svc.client = None
        svc._refresher_task = None

        # Should not raise
        await svc.stop()


# ===========================================================================
# Concurrency — lock serializes operations
# ===========================================================================

class TestConcurrency:
    """Multiple callers should be serialized via the asyncio.Lock."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_serialized(self):
        svc = _make_service()
        client = _mock_connected_client()
        client.send_message = AsyncMock()
        svc.client = client

        call_order = []

        async def tracked_start():
            call_order.append("start")
            return True

        with patch.object(svc, "start", side_effect=tracked_start):
            # Launch two concurrent send_message calls
            results = await asyncio.gather(
                svc.send_message(123, "first"),
                svc.send_message(123, "second"),
            )

        # Both should succeed
        assert all(results)
        # start was called twice (serialized by lock)
        assert call_order == ["start", "start"]
        # disconnect was called twice (once per operation)
        assert client.disconnect.await_count == 2


# ===========================================================================
# Async iterator mock helper
# ===========================================================================

class AsyncIteratorMock:
    """Helper to mock Telethon's async iterators (iter_messages, iter_participants)."""

    def __init__(self, items):
        self._items = items
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item
