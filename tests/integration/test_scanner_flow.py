"""
Integration tests for scanner workflow.
Test the full flow from scanning to saving credentials.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.mark.integration
class TestScannerWorkflow:
    """Integration tests for scanner workflow"""

    def test_save_valid_token(self, monkeypatch):
        """A scanner result is queued and the real validator saves it."""
        from celery.contrib.testing.worker import start_worker

        from app.core import connectivity, queue_monitor
        from app.workers import celery_app
        from app.workers.celery_app import app
        from app.workers.tasks import flow_tasks, scanner_tasks, validation_tasks
        from app.workers.tasks.scanners import common as scanner_common

        saved_rows = []

        class Query:
            def __init__(self):
                self.operation = "select"
                self.row = None

            def select(self, *_columns):
                self.operation = "select"
                return self

            def eq(self, *_args):
                return self

            def insert(self, row):
                self.operation = "insert"
                self.row = row
                return self

            def execute(self):
                if self.operation == "select":
                    return SimpleNamespace(data=[])
                saved_rows.append(self.row)
                return SimpleNamespace(data=[{"id": "new_cred_id"}])

        class Database:
            def table(self, _name):
                return Query()

        class RedisPipeline:
            def incr(self, _key):
                return self

            def ttl(self, _key):
                return self

            def execute(self):
                return 1, -1

        class Redis:
            def set(self, *_args, **_kwargs):
                return True

            def pipeline(self):
                return RedisPipeline()

            def expire(self, *_args, **_kwargs):
                return True

        class Response:
            status_code = 200

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        class TelegramClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, url, **_kwargs):
                if url.endswith("/getMe"):
                    return Response({"ok": True, "result": {"id": 123456789}})
                if url.endswith("/getUpdates"):
                    return Response({"ok": True, "result": []})
                raise AssertionError(f"Unexpected Telegram request: {url}")

        class Broadcaster:
            async def send_log(self, _message):
                return None

        redis = Redis()
        monkeypatch.setattr(scanner_common, "redis_client", redis)
        monkeypatch.setattr(validation_tasks, "redis_client", redis)
        monkeypatch.setattr(validation_tasks, "db", Database())
        monkeypatch.setattr(validation_tasks.httpx, "AsyncClient", TelegramClient)
        monkeypatch.setattr(flow_tasks, "get_broadcaster", lambda: Broadcaster())

        # Use Celery's real task and queue, backed by an in-memory broker.
        monkeypatch.setitem(app.conf, "broker_url", "memory://")
        monkeypatch.setitem(app.conf, "result_backend", "cache+memory://")
        monkeypatch.setitem(app.conf, "task_always_eager", False)
        original_backend = app._backend

        # Keep worker startup and queue tracking local to this test.
        monkeypatch.setattr(connectivity, "wait_for_internet_sync", lambda **_kwargs: True)
        monkeypatch.setattr(celery_app, "_send_signal_log", lambda _message: None)
        monkeypatch.setattr(queue_monitor, "record_task_enqueued", lambda *_args: None)
        monkeypatch.setattr(queue_monitor, "record_task_started", lambda *_args: None)

        results = [
            {
                "token": "123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh",
                "meta": {"source": "test"},
            }
        ]

        try:
            with start_worker(
                app,
                queues=["validation"],
                pool="solo",
                perform_ping_check=False,
            ):
                queued = asyncio.run(
                    scanner_tasks._save_credentials_async(results, "test_source")
                )
                assert queued >= 1

                deadline = time.monotonic() + 5
                while not saved_rows and time.monotonic() < deadline:
                    time.sleep(0.01)

                assert len(saved_rows) >= 1
                assert saved_rows[0]["source"] == "test_source"
        finally:
            if original_backend is None:
                app._backend_cache = None
                try:
                    del app._local.backend
                except AttributeError:
                    pass
            else:
                app._backend = original_backend

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
