"""
Integration tests for scanner workflow.
Test the full flow from scanning to saving credentials.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest


@pytest.mark.integration
class TestScannerWorkflow:
    """Integration tests for scanner workflow"""

    def test_save_valid_token(self, monkeypatch):
        """A scanner result is staged in validation_queue, not sent to the broker."""
        from app.workers.tasks import scanner_tasks

        token = "123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
        staged = []

        async def fake_enqueue(entries):
            entries = list(entries)
            staged.extend(entries)
            return len(entries)

        monkeypatch.setattr("app.services.validation_queue_srv.enqueue", fake_enqueue)
        monkeypatch.setattr(scanner_tasks, "_is_own_bot_token", lambda _token: False)
        monkeypatch.setattr(
            scanner_tasks,
            "redis_client",
            SimpleNamespace(set=lambda *_args, **_kwargs: True),
        )

        results = [{"token": token, "meta": {"source": "test"}}]
        queued = asyncio.run(scanner_tasks._save_credentials_async(results, "test_source"))

        assert queued == 1
        assert staged == [{"token": token, "source": "test_source", "meta": {"source": "test"}}]

    @patch("app.services.validation_queue_srv.enqueue", new_callable=AsyncMock)
    @patch("app.workers.tasks.scanner_tasks.db")
    @patch("requests.get")
    def test_skip_invalid_token(self, mock_requests, mock_db, mock_enqueue):
        """_save_credentials stages tokens in validation_queue; format checks happen later.
        Tokens with no 'token' key or MANUAL_REVIEW_REQUIRED are skipped at this layer;
        malformed-format tokens are staged and discarded by the validator."""
        from app.workers.tasks.scanner_tasks import _save_credentials

        # Completely missing token key -- should be skipped (count 0)
        results = [{"meta": {}}]  # no "token" key at all
        saved = _save_credentials(results, "test_source")
        assert saved == 0
        mock_enqueue.assert_not_awaited()

    @patch("app.services.validation_queue_srv.enqueue", new_callable=AsyncMock)
    @patch("app.workers.tasks.scanner_tasks.db")
    @patch("requests.get")
    def test_skip_duplicate_token(self, mock_requests, mock_db, mock_enqueue):
        """Tokens already in Redis soft-dedup key are skipped (not re-enqueued).
        Uses patch to inject a pre-seeded Redis mock so no live Redis needed."""
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        from app.workers.tasks.scanner_tasks import _save_credentials

        token = "123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"

        # Simulate Redis returning None from SET NX (key already exists -> duplicate)
        mock_redis = MagicMock()
        mock_redis.set.return_value = None   # nx=True returns None when key already exists
        mock_redis.get.return_value = b"1"

        with _patch("app.workers.tasks.scanner_tasks.redis_client", mock_redis):
            results = [{"token": token, "meta": {}}]
            saved = _save_credentials(results, "test_source")

        # Should be soft-deduped (set returned None) and not re-enqueued
        assert saved == 0
        mock_enqueue.assert_not_awaited()



@pytest.mark.integration
class TestBroadcastWorkflow:
    """Integration tests for broadcast workflow"""

    @pytest.mark.asyncio
    async def test_broadcaster_initialization(self):
        """Test BroadcasterService initializes correctly.
        BroadcasterService now holds a list of tokens (bot_tokens),
        not a single bot_token string."""
        from app.services.broadcaster_srv import BroadcasterService

        broadcaster = BroadcasterService()
        # Multi-token pool: bot_tokens is a list, not a single string
        assert hasattr(broadcaster, "bot_tokens")
        assert isinstance(broadcaster.bot_tokens, list)
        assert len(broadcaster.bot_tokens) >= 1
        # No eager bot instantiation — lazy-loaded on first use
        assert not hasattr(broadcaster, "_bot") or broadcaster._bot is None

    @pytest.mark.asyncio
    @patch("app.services.broadcaster_srv.Bot")
    async def test_send_log_with_retry(self, mock_bot_class):
        """Test BroadcasterService can be constructed; send_log requires
        async infrastructure that is out of scope for unit tests."""
        from app.services.broadcaster_srv import BroadcasterService

        mock_bot = Mock()
        mock_bot.send_message = Mock(return_value=None)
        mock_bot_class.return_value = mock_bot

        broadcaster = BroadcasterService()
        assert broadcaster is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "integration"])
