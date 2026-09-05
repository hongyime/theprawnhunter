import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# Add project root to sys.path so we can import 'app'
sys.path.append(str(Path(__file__).parent.parent))

# Mock Environment Variables BEFORE importing app
os.environ["PROJECT_NAME"] = "Test Hunter"
os.environ["ENV"] = "test"
os.environ["SUPABASE_URL"] = "https://example.supabase.co"
os.environ["SUPABASE_KEY"] = "mock-key"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "mock-service-role-key"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
# Generate a valid key for testing
valid_key = Fernet.generate_key().decode()
os.environ["ENCRYPTION_KEY"] = valid_key

os.environ["MONITOR_BOT_TOKEN"] = "123:ABC,456:DEF,789:GHI"
os.environ["MONITOR_GROUP_ID"] = "-100123"
os.environ["TELEGRAM_API_ID"] = "12345"
os.environ["TELEGRAM_API_HASH"] = "abc"
os.environ["MONITOR_API_KEY"] = "test-monitor-key-for-pytest"

from app.api.main import app  # noqa: E402, I001

@pytest.fixture(scope="module")
def client():
    # Use TestClient for API tests
    return TestClient(app)
