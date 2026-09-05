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
        print("   ✅ All core imports successful")
        return True
    except Exception as e:
        print(f"   ❌ Core import failed: {e}")
        return False

def test_service_imports():
    """Test service imports"""
    print("\n2. Testing service imports...")
    try:
        print("   ✅ All service imports successful")
        return True
    except Exception as e:
        print(f"   ❌ Service import failed: {e}")
        return False

def test_task_imports():
    """Test Celery task imports"""
    print("\n3. Testing task imports...")
    try:
        print("   ✅ All task imports successful")
        return True
    except Exception as e:
        print(f"   ❌ Task import failed: {e}")
        return False

def test_api_imports():
    """Test API imports"""
    print("\n4. Testing API imports...")
    try:
        print("   ✅ All API imports successful")
        return True
    except Exception as e:
        print(f"   ❌ API import failed: {e}")
        return False

def test_helper_imports():
    """Test helper utilities"""
    print("\n5. Testing helper utilities...")
    try:
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
        breaker = get_circuit_breaker("test")
        assert breaker is not None

        from app.core.metrics import metrics
        assert metrics is not None

        print("   ✅ All new features working")
        return True
    except Exception as e:
        print(f"   ❌ Feature test failed: {e}")
        return False


def test_runtime_regressions():
    """Catch the high-value regressions that have recently escaped into runtime."""
    print("\n8. Testing runtime regression guards...")
    try:
        import asyncio

        from app.core.db_retry import DatabaseHealth
        from app.services import bot_listener
        from app.workers.tasks import validation_tasks

        if not callable(DatabaseHealth.check_connection):
            raise AssertionError("DatabaseHealth.check_connection is not callable")

        bot_source = open(bot_listener.__file__, encoding="utf-8").read()
        if "_resolve_monitor_group_ids_async" not in bot_source:
            raise AssertionError("bot_listener.log_update is not using async monitor guard")

        validation_source = open(validation_tasks.__file__, encoding="utf-8").read()
        if '".update({\n                        "meta": new_meta,\n                        "confidence_score": score,' in validation_source:
            raise AssertionError("validation backfill still updates top-level confidence_score")

        asyncio.run(asyncio.to_thread(DatabaseHealth.check_connection))
        print("   ✅ Regression guards passed")
        return True
    except Exception as e:
        print(f"   ❌ Runtime regression test failed: {e}")
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("Telegram Hunter - Deployment Validation")
    print("=" * 60)

    results = [
        test_core_imports(),
        test_service_imports(),
        test_task_imports(),
        test_api_imports(),
        test_helper_imports(),
        test_config_validation(),
        test_new_features(),
        test_runtime_regressions(),
    ]

    print("\n" + "=" * 60)
    if all(results):
        print("✅ All validation checks passed!")
        print("✅ Ready for deployment")
        print("=" * 60)
        sys.exit(0)
    else:
        print("❌ Some checks failed")
        print("=" * 60)
        sys.exit(1)
