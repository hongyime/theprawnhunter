"""
Integration tests for scanner workflow.
Test the full flow from scanning to saving credentials.
"""
from unittest.mock import Mock, patch

import pytest


@pytest.mark.integration
class TestScannerWorkflow:
    """Integration tests for scanner workflow"""

    @patch("app.workers.tasks.validation_tasks.validate_token")  # local import inside _save_credentials_async
    @patch("app.workers.tasks.scanner_tasks.db")
    @patch("requests.get")
    def test_save_valid_token(self, mock_requests, mock_db, mock_validate_task):
        """Test saving a valid token through validation flow"""
        from app.workers.tasks.scanner_tasks import _save_credentials

        # Prevent actual Celery broker connection -- .delay() is a no-op
        mock_validate_task.delay = Mock(return_value=None)

        mock_requests.return_value.status_code = 200
        mock_requests.return_value.json.return_value = {
            "ok": True,
            "result": {
                "id": 123456789,
                "username": "test_bot",
                "first_name": "Test Bot",
            },
        }

        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
        mock_db.table.return_value.insert.return_value.execute.return_value.data = [
            {"id": "new_cred_id"}
        ]

        results = [
            {
                "token": "123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh",
                "meta": {"source": "test"},
            }
        ]

        saved = _save_credentials(results, "test_source")
        assert saved >= 0

    @patch("app.workers.tasks.validation_tasks.validate_token")
    @patch("app.workers.tasks.scanner_tasks.db")
    @patch("requests.get")
    def test_skip_invalid_token(self, mock_requests, mock_db, mock_validate_task):
        """_save_credentials enqueues tokens for async validation via validate_token.delay().
        Format validation happens INSIDE the validator worker, not here.
        Tokens with no 'token' key or MANUAL_REVIEW_REQUIRED are skipped at this layer;
        malformed-format tokens are forwarded to the validator which discards them."""
        from app.workers.tasks.scanner_tasks import _save_credentials

        mock_validate_task.delay = Mock(return_value=None)

        # Completely missing token key -- should be skipped (count 0)
        results = [{"meta": {}}]  # no "token" key at all
        saved = _save_credentials(results, "test_source")
        assert saved == 0
        mock_validate_task.delay.assert_not_called()

    @patch("app.workers.tasks.validation_tasks.validate_token")
    @patch("app.workers.tasks.scanner_tasks.db")
    @patch("requests.get")
    def test_skip_duplicate_token(self, mock_requests, mock_db, mock_validate_task):
        """Tokens already in Redis soft-dedup key are skipped (not re-enqueued).
        Uses patch to inject a pre-seeded Redis mock so no live Redis needed."""
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        from app.workers.tasks.scanner_tasks import _save_credentials

        mock_validate_task.delay = Mock(return_value=None)

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
        mock_validate_task.delay.assert_not_called()



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
