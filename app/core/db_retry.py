"""
Database retry wrapper for Supabase operations.
Handles transient connection errors with exponential backoff.
"""
import functools
from collections.abc import Callable
from typing import TypeVar

from app.core.logger import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


def _is_transient_db_error(e: Exception) -> bool:
    """Return True only for errors worth retrying (connection/timeout). Not 4xx logic errors."""
    msg = str(e).lower()
    transient_markers = ("connection", "timeout", "socket", "econnreset", "broken pipe",
                         "server disconnected", "eof occurred", "network")
    permanent_markers = ("unique", "violates", "not-null", "foreign key", "permission",
                         "relation", "column", "syntax", "invalid input", "42", "23")
    if any(p in msg for p in permanent_markers):
        return False
    return any(t in msg for t in transient_markers)


def with_db_retry(func: Callable[..., T]) -> Callable[..., T]:
    """
    Decorator for database operations with retry logic.
    Only retries transient connection/timeout errors, not permanent constraint violations.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(1, 4):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exc = e
                if not _is_transient_db_error(e):
                    raise  # Do not retry permanent errors
                if attempt < 3:
                    delay = min(1.0 * (2 ** (attempt - 1)), 5.0)
                    logger.warning(f"Database transient error in {func.__name__} (attempt {attempt}/3), retry in {delay:.1f}s: {e}")
                    import time as _time
                    _time.sleep(delay)
                else:
                    logger.error(f"Database failed after 3 attempts in {func.__name__}: {e}", exc_info=True)
                    raise
        raise last_exc  # unreachable but satisfies type checker

    return wrapper



class DatabaseHealth:
    """Database health check utilities"""

    @staticmethod
    @with_db_retry
    def check_connection() -> bool:
        """
        Verify database connectivity.
        Returns True if successful, raises exception if fails after retries.
        """
        from app.core.database import db

        # Simple ping query
        db.table("discovered_credentials").select("id").limit(1).execute()

        logger.info("Database health check passed")
        return True

    @staticmethod
    def get_pool_stats() -> dict:
        """Get connection pool statistics (if available)"""
        # Supabase Python client doesn't expose pool stats directly
        # This is a placeholder for future implementation
        return {
            "status": "unknown",
            "note": "Pool stats not available in current Supabase client"
        }
