"""
Validate configuration and dependencies at startup.
Run this before deploying to catch configuration issues early.
"""
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def validate_config():
    """Validate configuration settings"""
    print("1. Validating configuration...")
    try:
        from app.core.config import settings
        print(f"   ✅ Configuration loaded: {settings.PROJECT_NAME}")
        print(f"   ✅ Environment: {settings.ENV}")
        print(f"   ✅ Supabase URL: {settings.SUPABASE_URL[:30]}...")
        print(f"   ✅ Redis URL: {settings.REDIS_URL[:20]}...")
        return True
    except Exception as e:
        print(f"   ❌ Config validation failed: {e}")
        return False

def validate_database():
    """Validate database connection"""
    print("\n2. Validating database connection...")
    try:
        from app.core.database import db
        # Try a simple query
        db.table("discovered_credentials").select("id").limit(1).execute()
        print("   ✅ Database connected successfully")
        return True
    except Exception as e:
        print(f"   ❌ Database connection failed: {e}")
        return False

def validate_runtime_guards():
    """Validate code paths that have recently broken in production-like runs."""
    print("\n4. Validating runtime guards...")
    try:
        from app.core.db_retry import DatabaseHealth
        from app.services import bot_listener
        from app.workers.tasks import validation_tasks

        if not callable(DatabaseHealth.check_connection):
            raise TypeError("DatabaseHealth.check_connection is not callable")

        with open(bot_listener.__file__, encoding="utf-8") as fh:
            bot_source = fh.read()
        if "_resolve_monitor_group_ids_async" not in bot_source:
            raise AssertionError("bot_listener.log_update is not using async monitor guard")

        with open(validation_tasks.__file__, encoding="utf-8") as fh:
            validation_source = fh.read()
        if '"confidence_score": score,' in validation_source and '".update({' in validation_source:
            marker = '.update({\n                        "meta": new_meta,\n                        "confidence_score": score,'
            if marker in validation_source:
                raise AssertionError("validation backfill still updates top-level confidence_score")

        print("   ✅ Runtime guards look correct")
        return True
    except Exception as e:
        print(f"   ❌ Runtime guard validation failed: {e}")
        return False

def validate_redis():
    """Validate Redis connection"""
    print("\n3. Validating Redis connection...")
    try:
        import redis

        from app.core.config import settings
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        client.ping()
        print("   ✅ Redis connected successfully")
        return True
    except Exception as e:
        print(f"   ❌ Redis connection failed: {e}")
        return False

def validate_telegram_api():
    """Validate Telegram Bot API"""
    print("\n5. Validating Telegram Bot API...")
    try:
        import requests

        from app.core.config import settings

        url = f"https://api.telegram.org/bot{settings.MONITOR_BOT_TOKEN}/getMe"
        response = requests.get(url, timeout=10)

        if response.status_code == 200 and response.json().get('ok'):
            bot_info = response.json()['result']
            print(f"   ✅ Bot API connected: @{bot_info.get('username')}")
            return True
        else:
            print(f"   ❌ Bot API failed: {response.text}")
            return False
    except Exception as e:
        print(f"   ❌ Telegram API validation failed: {e}")
        return False

def validate_optional_services():
    """Check optional API keys"""
    print("\n6. Checking optional services...")
    from app.core.config import settings

    github_configured = bool(settings.GITHUB_TOKEN or settings.GITHUB_TOKENS)
    fofa_api_configured = bool(settings.FOFA_KEY and settings.FOFA_EMAIL)

    services = [
        ("Shodan", bool(settings.SHODAN_KEY), "Configured", "Not configured"),
        ("URLScan", bool(settings.URLSCAN_KEY), "Configured", "Not configured"),
        (
            "GitHub",
            github_configured,
            "Configured (GITHUB_TOKEN or GITHUB_TOKENS)",
            "Not configured",
        ),
        (
            "FOFA API",
            fofa_api_configured,
            "Configured",
            "Not configured (optional; web / extension mode is fine)",
        ),
    ]

    for name, is_configured, configured_msg, missing_msg in services:
        status = "✅" if is_configured else "ℹ️ "
        print(f"   {status} {name}: {configured_msg if is_configured else missing_msg}")

    return True

if __name__ == "__main__":
    print("=" * 60)
    print("Telegram Hunter - Startup Validation")
    print("=" * 60)

    results = [
        validate_config(),
        validate_database(),
        validate_runtime_guards(),
        validate_redis(),
        validate_telegram_api(),
        validate_optional_services()
    ]

    print("\n" + "=" * 60)
    if all(results[:4]):  # Only require first 4 to pass
        print("✅ All critical validations passed!")
        print("=" * 60)
        sys.exit(0)
    else:
        print("❌ Some validations failed. Please fix configuration.")
        print("=" * 60)
        sys.exit(1)
