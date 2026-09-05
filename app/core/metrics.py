"""
Metrics collection for monitoring task performance.
Tracks execution times, success/failure rates, and service health.

Persistence: counters (inc) are flushed to Redis periodically via flush_to_redis().
Called from the heartbeat task every 30 min so restarts don't lose all history.
Redis keys: metrics:counter:<name>  (INCRBY, no TTL — survives restarts)
            metrics:flush_ts         (last flush timestamp)
"""
import functools
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from app.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MetricData:
    """Container for metric data"""
    count: int = 0
    total_time: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    last_execution: float | None = None
    min_time: float = float('inf')
    max_time: float = 0.0

    def record_success(self, duration: float):
        """Record successful execution"""
        self.count += 1
        self.success_count += 1
        self.total_time += duration
        self.last_execution = time.time()
        self.min_time = min(self.min_time, duration)
        self.max_time = max(self.max_time, duration)

    def record_failure(self, duration: float):
        """Record failed execution"""
        self.count += 1
        self.failure_count += 1
        self.total_time += duration
        self.last_execution = time.time()

    @property
    def avg_time(self) -> float:
        """Calculate average execution time"""
        return self.total_time / self.count if self.count > 0 else 0.0

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage"""
        return (self.success_count / self.count * 100) if self.count > 0 else 0.0


class MetricsCollector:
    """
    Centralized metrics collection with Redis persistence for counters.

    In-memory state tracks execution timing (min/max/avg).
    Counter totals (count, success_count, failure_count) are periodically
    flushed to Redis via flush_to_redis() so they survive worker restarts.
    The heartbeat task (every 30 min) calls this automatically.
    """

    def __init__(self):
        self._metrics: dict[str, MetricData] = defaultdict(MetricData)
        # Track counts added since last flush so flush uses INCRBY not SET
        self._pending_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # Cached Redis client — initialised once on first flush/get_all_metrics call
        self._redis_client = None

    def track(self, metric_name: str):
        """
        Decorator to track function execution metrics.

        Example:
            @metrics.track("scan_shodan")
            def scan_shodan():
                # ... code ...
        """
        def decorator(func):
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    duration = time.time() - start_time
                    self.record_success(metric_name, duration)
                    return result
                except Exception:
                    duration = time.time() - start_time
                    self.record_failure(metric_name, duration)
                    raise

            # Support async functions
            import asyncio
            if asyncio.iscoroutinefunction(func):
                @functools.wraps(func)
                async def async_wrapper(*args, **kwargs):
                    start_time = time.time()
                    try:
                        result = await func(*args, **kwargs)
                        duration = time.time() - start_time
                        self.record_success(metric_name, duration)
                        return result
                    except Exception:
                        duration = time.time() - start_time
                        self.record_failure(metric_name, duration)
                        raise
                return async_wrapper

            return sync_wrapper
        return decorator

    def record_success(self, metric_name: str, duration: float):
        """Record successful operation"""
        self._metrics[metric_name].record_success(duration)
        self._pending_counts[metric_name]["count"] += 1
        self._pending_counts[metric_name]["success_count"] += 1
        logger.debug(f"Metric [{metric_name}] success in {duration:.2f}s")

    def record_failure(self, metric_name: str, duration: float):
        """Record failed operation"""
        self._metrics[metric_name].record_failure(duration)
        self._pending_counts[metric_name]["count"] += 1
        self._pending_counts[metric_name]["failure_count"] += 1
        logger.warning(f"Metric [{metric_name}] failure after {duration:.2f}s")

    def inc(self, metric_name: str, amount: int = 1):
        """Increment a counter-style metric (no duration)."""
        self._metrics[metric_name].count += amount
        self._pending_counts[metric_name]["count"] += amount
        logger.debug(f"Metric [{metric_name}] inc by {amount}")

    def get_metric(self, metric_name: str) -> MetricData | None:
        """Get specific metric data"""
        return self._metrics.get(metric_name)

    def flush_to_redis(self) -> bool:
        """
        Persist pending counter increments to Redis via INCRBY.
        Safe to call from any worker — uses INCRBY so concurrent flushes
        from multiple workers accumulate correctly.
        Returns True on success, False if Redis unavailable.
        """
        if not self._pending_counts:
            return True
        try:
            r = self._get_redis()
            if r is None:
                logger.warning("[Metrics] Redis client unavailable — flush skipped")
                return False
            pipe = r.pipeline()
            for metric_name, counters in list(self._pending_counts.items()):
                for counter_key, delta in counters.items():
                    if delta > 0:
                        pipe.incrby(f"metrics:counter:{metric_name}:{counter_key}", delta)
            pipe.set("metrics:flush_ts", time.time())
            pipe.execute()
            self._pending_counts.clear()
            return True
        except Exception as e:
            logger.warning(f"[Metrics] Redis flush failed (non-fatal): {e}")
            return False

    def _get_redis(self):
        """Returns cached Redis client, initialised once."""
        if self._redis_client is None:
            try:
                import redis as _redis

                from app.core.config import settings
                self._redis_client = _redis.from_url(settings.REDIS_URL, decode_responses=True)
            except Exception as e:
                logger.warning(f"[Metrics] Could not create Redis client: {e}")
                return None
        return self._redis_client

    def get_all_metrics(self) -> dict[str, dict]:
        """Get all metrics as dict — merges in-memory + Redis persisted totals."""
        # Try to load Redis totals and merge
        redis_totals: dict[str, dict[str, int]] = {}
        try:
            r = self._get_redis()
            if r:
                keys = r.keys("metrics:counter:*")
                for key in keys:
                    # key format: metrics:counter:<name>:<field>
                    parts = key.split(":", 3)
                    if len(parts) == 4:
                        _, _, name, field_name = parts
                        val = r.get(key)
                        if val:
                            redis_totals.setdefault(name, {})[field_name] = int(val)
        except Exception:
            pass  # Redis unavailable — show in-memory only

        result = {}
        all_names = set(self._metrics.keys()) | set(redis_totals.keys())
        for name in all_names:
            data = self._metrics.get(name)
            redis_data = redis_totals.get(name, {})
            # Use max of in-memory vs Redis for counts (Redis is cumulative across restarts)
            count = max(data.count if data else 0, redis_data.get("count", 0))
            success = max(data.success_count if data else 0, redis_data.get("success_count", 0))
            failure = max(data.failure_count if data else 0, redis_data.get("failure_count", 0))
            result[name] = {
                "count": count,
                "success_count": success,
                "failure_count": failure,
                "success_rate": round((success / count * 100) if count > 0 else 0.0, 2),
                "avg_time": round(data.avg_time, 2) if data else 0.0,
                "min_time": round(data.min_time, 2) if data and data.min_time != float('inf') else None,
                "max_time": round(data.max_time, 2) if data else 0.0,
                "last_execution": datetime.fromtimestamp(data.last_execution).isoformat() if data and data.last_execution else None,
                "source": "redis+memory" if redis_data else "memory",
            }
        return result

    def reset(self):
        """Reset all in-memory metrics (does not clear Redis)"""
        self._metrics.clear()
        self._pending_counts.clear()
        logger.info("All in-memory metrics reset")

    def get_summary(self) -> dict:
        """Get summary statistics"""
        total_executions = sum(m.count for m in self._metrics.values())
        total_successes = sum(m.success_count for m in self._metrics.values())
        total_failures = sum(m.failure_count for m in self._metrics.values())

        return {
            "total_executions": total_executions,
            "total_successes": total_successes,
            "total_failures": total_failures,
            "overall_success_rate": round((total_successes / total_executions * 100) if total_executions > 0 else 0, 2),
            "tracked_functions": len(self._metrics),
        }


# Global metrics instance
metrics = MetricsCollector()
