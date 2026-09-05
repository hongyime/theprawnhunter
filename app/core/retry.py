import asyncio
import functools
import time
from collections.abc import Callable
from typing import TypeVar

from app.core.logger import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential: bool = True,
    exceptions: tuple[type[Exception], ...] = (Exception,)
):
    """
    Retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Initial delay between retries in seconds
        max_delay: Maximum delay between retries
        exponential: Use exponential backoff if True, constant delay otherwise
        exceptions: Tuple of exception types to catch and retry

    Example:
        @retry(max_attempts=3, base_delay=2.0)
        def fetch_data():
            # ... code that might fail ...

        @retry(max_attempts=5, exceptions=(ConnectionError, TimeoutError))
        async def async_fetch():
            # ... async code ...
    """
    def decorator(func: Callable) -> Callable:
        is_async = asyncio.iscoroutinefunction(func)

        if is_async:
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                last_exception = None

                for attempt in range(1, max_attempts + 1):
                    try:
                        return await func(*args, **kwargs)
                    except exceptions as e:
                        last_exception = e

                        if attempt == max_attempts:
                            logger.error(
                                f"Function {func.__name__} failed after {max_attempts} attempts",
                                exc_info=True
                            )
                            raise

                        # Calculate delay
                        if exponential:
                            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        else:
                            delay = base_delay

                        logger.warning(
                            f"Function {func.__name__} failed (attempt {attempt}/{max_attempts}). "
                            f"Retrying in {delay:.1f}s. Error: {str(e)}"
                        )

                        await asyncio.sleep(delay)

                # Should never reach here, but type checker needs this
                raise last_exception if last_exception else RuntimeError("Unexpected retry state")

            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                last_exception = None

                for attempt in range(1, max_attempts + 1):
                    try:
                        return func(*args, **kwargs)
                    except exceptions as e:
                        last_exception = e

                        if attempt == max_attempts:
                            logger.error(
                                f"Function {func.__name__} failed after {max_attempts} attempts",
                                exc_info=True
                            )
                            raise

                        # Calculate delay
                        if exponential:
                            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        else:
                            delay = base_delay

                        logger.warning(
                            f"Function {func.__name__} failed (attempt {attempt}/{max_attempts}). "
                            f"Retrying in {delay:.1f}s. Error: {str(e)}"
                        )

                        time.sleep(delay)

                # Should never reach here, but type checker needs this
                raise last_exception if last_exception else RuntimeError("Unexpected retry state")

            return sync_wrapper

    return decorator


def retry_on_telegram_error(max_attempts: int = 3):
    """
    Specialized retry decorator for Telegram API errors.
    Handles rate limits (429/RetryAfter) by honoring Telegram's retry_after hint.
    """
    import asyncio as _asyncio
    import functools

    from telegram.error import NetworkError, RetryAfter, TimedOut

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except RetryAfter as e:
                    last_exc = e
                    wait = getattr(e, "retry_after", None) or 30
                    logger.warning(
                        f"Telegram RetryAfter in {func.__name__} (attempt {attempt}/{max_attempts}). "
                        f"Sleeping {wait}s as instructed by Telegram."
                    )
                    if attempt < max_attempts:
                        await _asyncio.sleep(wait)
                    else:
                        raise
                except (TimedOut, NetworkError) as e:
                    last_exc = e
                    if attempt == max_attempts:
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts: {e}", exc_info=True)
                        raise
                    delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
                    logger.warning(
                        f"{func.__name__} failed (attempt {attempt}/{max_attempts}). Retrying in {delay:.1f}s. Error: {e}"
                    )
                    await _asyncio.sleep(delay)
            raise last_exc if last_exc else RuntimeError("Unexpected retry state")
        return wrapper
    return decorator


def retry_on_connection_error(max_attempts: int = 3):
    """
    Retry decorator for connection-related errors.
    Useful for database and HTTP requests.
    """
    import requests
    from httpx import ConnectError, TimeoutException

    return retry(
        max_attempts=max_attempts,
        base_delay=1.0,
        max_delay=10.0,
        exponential=True,
        exceptions=(
            ConnectionError,
            TimeoutError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ConnectError,
            TimeoutException
        )
    )
