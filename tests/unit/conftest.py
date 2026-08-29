"""
Unit-test conftest: mock heavy infrastructure modules before any unit test
imports them, so tests run without a live Redis / Celery broker.
"""
import os
import sys
from unittest.mock import MagicMock

# ── Environment stubs (must be set before app.core.config is imported) ──────
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "mock-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "mock-service-role-key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENCRYPTION_KEY", "B" * 43 + "=")
os.environ.setdefault("MONITOR_BOT_TOKEN", "123:ABC")
os.environ.setdefault("MONITOR_GROUP_ID", "-100123")
os.environ.setdefault("MONITOR_API_KEY", "test-monitor-key-for-pytest")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "abc")

# ── Stub app.workers.celery_app BEFORE any task module is imported ───────────
# This prevents Celery from trying to connect to Redis at import time.
if "app.workers.celery_app" not in sys.modules:
    _fake_celery_app = MagicMock()
    # Make @app.task(...) work as a no-op decorator
    _fake_celery_app.task = lambda *a, **kw: (lambda f: f)
    _mock_celery_module = MagicMock()
    _mock_celery_module.app = _fake_celery_app
    _mock_celery_module.get_worker_loop = MagicMock()
    _mock_celery_module._run_sync = MagicMock()
    sys.modules["app.workers.celery_app"] = _mock_celery_module
