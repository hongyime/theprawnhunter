"""
Comprehensive validation script for deployment readiness.
Checks imports, syntax, and core functionality.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_core_imports():
    """Test all core module imports"""
    print("1. Testing core imports...")
    try:
        from app.core.config import settings
        from app.core.database import db
        from app.core.security import encrypt_token, decrypt_token
        from app.core.redis_srv import RedisService
        from app.core.logger import get_logger
        from app.core.retry import retry
        from app.core.circuit_breaker import get_circuit_breaker
        from app.core.metrics import MetricsCollector
        from app.core.audit import AuditLogger
        print("   ✅ All core imports successful")
        return True
    except Exception as e:
        print(f"   ❌ Core import failed: {e}")
        return False

def test_service_imports():
    """Test service imports"""
    print("\n2. Testing service imports...")
    try:
        from app.services.scanners import ShodanService
        from app.services.bot_manager_srv import BotClientManager
        from app.services.scraper_srv import ScraperService
        from app.services.broadcaster_srv import BroadcasterService
        print("   ✅ All service imports successful")
        return True
    except Exception as e:
        print(f"   ❌ Service import failed: {e}")
        return False

def test_task_imports():
    """Test Celery task imports"""
    print("\n3. Testing task imports...")
    try:
        from app.workers.tasks.flow_tasks import (
            enrich_credential,
            exfiltrate_history,
            broadcast_finding,
        )
        from app.workers.tasks.scanner_tasks import run_shodan_scan
        from app.workers.tasks.audit_tasks import audit_active_topics
        print("   ✅ All task imports successful")
        return True
    except Exception as e:
        print(f"   ❌ Task import failed: {e}")
        return False

def test_api_imports():
    """Test API imports"""
    print("\n4. Testing API imports...")
    try:
        from app.api.main import app
        from app.api.routers.health import router as health_router
        from app.api.routers.monitor import router as monitor_router
        from app.api.routers.ingest import router as ingest_router
        print("   ✅ All API imports successful")
        return True
    except Exception as e:
        print(f"   ❌ API import failed: {e}")
        return False

def test_helper_imports():
    """Test helper utilities"""
    print("\n5. Testing helper utilities...")
    try:
        from app.utils.helpers import validate_token, extract_chat_id
        from app.utils.http_client import AsyncHttpClient
        print("   ✅ Helper utilities imported")
        return True
    except Exception as e:
        print(f"   ❌ Helper import failed: {e}")
        return False

def test_config_validation():
    """Test configuration"""
    print("\n6. Testing configuration...")
    try:
        from app.core.config import settings
        assert settings.PROJECT_NAME is not None
        assert settings.SUPABASE_URL is not None
        assert settings.REDIS_URL is not None
        assert len(settings.TARGET_COUNTRIES) > 0

        # MONITOR_API_KEY is required — /monitor and /health/detailed are unprotected without it
        if not settings.MONITOR_API_KEY:
            print("   ❌ MONITOR_API_KEY is not set — /monitor endpoints are unprotected!")
            return False

        print(f"   ✅ Config valid ({len(settings.TARGET_COUNTRIES)} countries, MONITOR_API_KEY set)")

        # Non-fatal advisory: Supabase RLS
        print("   ⚠️  ADVISORY: Verify Supabase RLS is enabled on 'exfiltrated_messages' and")
        print("      'discovered_credentials'. The anon key is embedded in the frontend bundle.")
        print("      Without RLS, anyone with the anon key can query all data directly.")

        return True
    except Exception as e:
        print(f"   ❌ Config validation failed: {e}")
        return False

def test_new_features():
    """Test new Phase 1-4 features"""
    print("\n7. Testing new features...")
    try:
        from app.core.logger import get_logger
        get_logger("test")

        from app.core.retry import retry
        @retry(max_attempts=1)
        def test_func():
            return True
        assert test_func() is True

        from app.core.circuit_breaker import get_circuit_breaker
        cb = get_circuit_breaker("test_service")
        assert cb is not None

        from app.core.metrics import MetricsCollector
        metrics = MetricsCollector()
        metrics.increment("test")
        
        print("   ✅ New features validated")
        return True
    except Exception as e:
        print(f"   ❌ New features test failed: {e}")
        return False

def test_security():
    """Test security components"""
    print("\n8. Testing security...")
    try:
        from app.core.security import encrypt_token, decrypt_token
        
        # Test encryption/decryption roundtrip
        test_token = "123456789:AAHXXXXXXXXXXXXXXXXXXXXXXXXXXX"
        encrypted = encrypt_token(test_token)
        decrypted = decrypt_token(encrypted)
        assert decrypted == test_token, "Encryption roundtrip failed"
        
        # Ensure encrypted is different from plaintext
        assert encrypted != test_token, "Token not encrypted!"
        
        print("   ✅ Security validation passed")
        return True
    except Exception as e:
        print(f"   ❌ Security test failed: {e}")
        return False

def main():
    """Run all validation tests"""
    print("=" * 60)
    print(" Deployment Validation Script")
    print("=" * 60)
    
    all_passed = True
    
    # Run all tests
    all_passed &= test_core_imports()
    all_passed &= test_service_imports()
    all_passed &= test_task_imports()
    all_passed &= test_api_imports()
    all_passed &= test_helper_imports()
    all_passed &= test_config_validation()
    all_passed &= test_new_features()
    all_passed &= test_security()
    
    print("\n" + "=" * 60)
    if all_passed:
        print(" ✅ All validation checks passed")
        print("=" * 60)
        return 0
    else:
        print(" ❌ Some validation checks failed")
        print("=" * 60)
        return 1

if __name__ == "__main__":
    sys.exit(main())
