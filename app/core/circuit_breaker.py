"""
Circuit breaker pattern for external service calls.
Prevents cascading failures by temporarily disabling failing services.
"""
import contextlib
import functools
import json as _json
import time
from collections.abc import Callable
from enum import Enum
from typing import TypeVar

from app.core.logger import get_logger

logger = get_logger(__name__)

T = TypeVar('T')

# Redis key prefix for cluster-wide circuit breaker state.
_CB_REDIS_PREFIX = "circuit_breaker:"
_CB_TRANSITION_LUA = """
local raw = redis.call('get', KEYS[1])
local s = raw and cjson.decode(raw) or {state='closed', failure_count=0, success_count=0}
local event = ARGV[1]
if event == 'failure' then
  s.failure_count = (s.failure_count or 0) + 1
  s.last_failure_time = tonumber(ARGV[4])
  if s.state == 'half_open' or s.failure_count >= tonumber(ARGV[2]) then
    s.state = 'open'; s.success_count = 0
  end
elseif event == 'success' then
  if s.state == 'half_open' then
    s.success_count = (s.success_count or 0) + 1
    if s.success_count >= tonumber(ARGV[3]) then
      s.state = 'closed'; s.failure_count = 0; s.success_count = 0
    end
  else
    s.failure_count = 0
  end
end
redis.call('set', KEYS[1], cjson.encode(s))
return cjson.encode(s)
"""


def _redis_client():
    """Lazy accessor — avoids import-time circular deps."""
    from app.core.redis_srv import redis_srv
    return redis_srv.client


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Service disabled due to failures
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker implementation for external service protection.
    State is stored in Redis so it is shared cluster-wide across API and
    worker processes.  Falls back to in-memory state when Redis is
    unavailable so that the breaker never blocks startup or crashes a
    request because Redis is down.

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

        # In-memory fallbacks — used only when Redis is unreachable.
        self._mem_state = CircuitState.CLOSED
        self._mem_failure_count = 0
        self._mem_success_count = 0
        self._mem_last_failure_time: float | None = None
        self._lock = __import__("threading").Lock()

    # ------------------------------------------------------------------
    # Redis helpers
    # ------------------------------------------------------------------

    def _redis_key(self) -> str:
        return f"{_CB_REDIS_PREFIX}{self.name}"

    def _load_state(self) -> dict:
        """Load state from Redis, falling back to in-memory."""
        try:
            raw = _redis_client().get(self._redis_key())
            if raw:
                return _json.loads(raw)
        except Exception:
            pass
        # Fallback: return in-memory state
        return {
            "state": self._mem_state.value,
            "failure_count": self._mem_failure_count,
            "success_count": self._mem_success_count,
            "last_failure_time": self._mem_last_failure_time,
        }

    def _save_state(self, data: dict) -> None:
        """Persist state to Redis; update in-memory mirror."""
        self._mem_state = CircuitState(data["state"])
        self._mem_failure_count = data.get("failure_count", 0)
        self._mem_success_count = data.get("success_count", 0)
        self._mem_last_failure_time = data.get("last_failure_time")
        with contextlib.suppress(Exception):
            _redis_client().set(
                self._redis_key(),
                _json.dumps(data, default=str),
            )

    def _atomic_transition(self, event: str) -> dict | None:
        try:
            raw = _redis_client().eval(
                _CB_TRANSITION_LUA, 1, self._redis_key(), event,
                self.failure_threshold, self.success_threshold, time.time(),
            )
            return _json.loads(raw)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # State property
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        data = self._load_state()
        return CircuitState(data.get("state", "closed"))

    @state.setter
    def state(self, new: CircuitState) -> None:
        data = self._load_state()
        data["state"] = new.value
        self._save_state(data)

    # ------------------------------------------------------------------
    # Decorator
    # ------------------------------------------------------------------

    def call(self, func: Callable[..., T]) -> Callable[..., T]:
        """Decorator to protect a sync or async function with circuit breaker."""
        import asyncio as _asyncio

        if _asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                cur = self.state
                if cur == CircuitState.OPEN:
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
            cur = self.state
            if cur == CircuitState.OPEN:
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
        data = self._load_state()
        last = data.get("last_failure_time")
        if last is None:
            return True
        return time.time() - float(last) >= self.recovery_timeout

    def _on_success(self):
        """Handle successful call"""
        updated = self._atomic_transition("success")
        if updated is not None:
            self._save_memory(updated)
            return
        with self._lock:
            data = self._load_state()
            cur = CircuitState(data.get("state", "closed"))
            if cur == CircuitState.HALF_OPEN:
                sc = data.get("success_count", 0) + 1
                if sc >= self.success_threshold:
                    logger.info(f"Circuit breaker [{self.name}] recovered (CLOSED)")
                    data["state"] = CircuitState.CLOSED.value
                    data["failure_count"] = 0
                    data["success_count"] = 0
                else:
                    data["success_count"] = sc
            else:
                # Reset failure count on success in CLOSED state
                data["failure_count"] = 0
            self._save_state(data)

    def _on_failure(self):
        """Handle failed call"""
        updated = self._atomic_transition("failure")
        if updated is not None:
            self._save_memory(updated)
            return
        with self._lock:
            data = self._load_state()
            fc = data.get("failure_count", 0) + 1
            data["failure_count"] = fc
            data["last_failure_time"] = time.time()
            cur = CircuitState(data.get("state", "closed"))

            if cur == CircuitState.HALF_OPEN:
                # Failed during recovery attempt
                logger.warning(f"Circuit breaker [{self.name}] recovery failed, reopening")
                data["state"] = CircuitState.OPEN.value
                data["success_count"] = 0
            elif fc >= self.failure_threshold:
                logger.error(
                    f"Circuit breaker [{self.name}] OPENED after {fc} failures"
                )
                data["state"] = CircuitState.OPEN.value
            self._save_state(data)

    def _save_memory(self, data: dict) -> None:
        self._mem_state = CircuitState(data["state"])
        self._mem_failure_count = data.get("failure_count", 0)
        self._mem_success_count = data.get("success_count", 0)
        self._mem_last_failure_time = data.get("last_failure_time")

    def reset(self):
        """Manually reset circuit breaker"""
        with self._lock:
            logger.info(f"Circuit breaker [{self.name}] manually reset")
            data = {
                "state": CircuitState.CLOSED.value,
                "failure_count": 0,
                "success_count": 0,
                "last_failure_time": None,
            }
            self._save_state(data)

    def get_status(self) -> dict:
        """Get current circuit breaker status"""
        data = self._load_state()
        return {
            "name": self.name,
            "state": data.get("state", "closed"),
            "failure_count": data.get("failure_count", 0),
            "success_count": data.get("success_count", 0),
            "last_failure_time": data.get("last_failure_time"),
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


import threading as _threading  # noqa: E402 — intentional deferred import

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
