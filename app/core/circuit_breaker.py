"""
Circuit breaker pattern for external service calls.
Prevents cascading failures by temporarily disabling failing services.
"""
import functools
import time
from collections.abc import Callable
from enum import Enum
from typing import TypeVar

from app.core.logger import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Service disabled due to failures
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker implementation for external service protection.
    Thread-safe via threading.Lock (Celery prefork workers each get their own instance).
    Note: state is per-process — not cluster-wide. Each Celery worker has independent state.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests fail immediately
    - HALF_OPEN: Testing recovery, allow limited requests

    Example:
        breaker = CircuitBreaker(
            name="shodan_api",
            failure_threshold=5,
            recovery_timeout=60
        )

        @breaker.call
        def fetch_shodan():
            # ... API call ...
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        success_threshold: int = 2
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: float | None = None
        self._lock = __import__("threading").Lock()  # Thread-safe state mutations

    def call(self, func: Callable[..., T]) -> Callable[..., T]:
        """Decorator to protect a sync or async function with circuit breaker."""
        import asyncio as _asyncio

        if _asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                if self.state == CircuitState.OPEN:
                    if self._should_attempt_reset():
                        logger.info(f"Circuit breaker [{self.name}] attempting recovery (HALF_OPEN)")
                        self.state = CircuitState.HALF_OPEN
                    else:
                        raise CircuitBreakerError(
                            f"Circuit breaker [{self.name}] is OPEN. Service temporarily disabled."
                        )
                try:
                    result = await func(*args, **kwargs)
                    self._on_success()
                    return result
                except CircuitBreakerError:
                    raise
                except Exception:
                    self._on_failure()
                    raise
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    logger.info(f"Circuit breaker [{self.name}] attempting recovery (HALF_OPEN)")
                    self.state = CircuitState.HALF_OPEN
                else:
                    raise CircuitBreakerError(
                        f"Circuit breaker [{self.name}] is OPEN. Service temporarily disabled."
                    )
            try:
                result = func(*args, **kwargs)
                self._on_success()
                return result
            except CircuitBreakerError:
                raise
            except Exception:
                self._on_failure()
                raise
        return wrapper

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt recovery"""
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.recovery_timeout

    def _on_success(self):
        """Handle successful call"""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.success_threshold:
                    logger.info(f"Circuit breaker [{self.name}] recovered (CLOSED)")
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.success_count = 0
            else:
                # Reset failure count on success in CLOSED state
                self.failure_count = 0

    def _on_failure(self):
        """Handle failed call"""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Failed during recovery attempt
                logger.warning(f"Circuit breaker [{self.name}] recovery failed, reopening")
                self.state = CircuitState.OPEN
                self.success_count = 0
            elif self.failure_count >= self.failure_threshold:
                logger.error(
                    f"Circuit breaker [{self.name}] OPENED after {self.failure_count} failures"
                )
                self.state = CircuitState.OPEN

    def reset(self):
        """Manually reset circuit breaker"""
        with self._lock:
            logger.info(f"Circuit breaker [{self.name}] manually reset")
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.last_failure_time = None

    def get_status(self) -> dict:
        """Get current circuit breaker status"""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure_time": self.last_failure_time
        }


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open"""
    pass


# Global circuit breakers for external services
# Thresholds match PRD §6.2: failure_threshold=5, recovery_timeout=60s (BUG-016)
_circuit_breakers = {
    "shodan": CircuitBreaker("shodan", failure_threshold=5, recovery_timeout=60),
    "urlscan": CircuitBreaker("urlscan", failure_threshold=5, recovery_timeout=60),
    "github": CircuitBreaker("github", failure_threshold=5, recovery_timeout=60),
    "fofa": CircuitBreaker("fofa", failure_threshold=5, recovery_timeout=60),
}


import threading as _threading

_circuit_breakers_lock = _threading.Lock()


def get_circuit_breaker(service_name: str) -> CircuitBreaker:
    """
    Get or lazily create a circuit breaker for a service.
    Thread-safe via module-level lock to prevent duplicate instantiation
    under concurrent first-call from multiple Celery worker threads.
    """
    # Fast path: already exists (no lock needed for read on CPython due to GIL,
    # but dict membership test is not atomic under concurrent writes, so check twice).
    if service_name in _circuit_breakers:
        return _circuit_breakers[service_name]
    with _circuit_breakers_lock:
        if service_name not in _circuit_breakers:
            _circuit_breakers[service_name] = CircuitBreaker(
                service_name,
                failure_threshold=5,
                recovery_timeout=60
            )
    return _circuit_breakers[service_name]


def get_all_circuit_status() -> dict:
    """Get status of all circuit breakers"""
    return {
        name: breaker.get_status()
        for name, breaker in _circuit_breakers.items()
    }
