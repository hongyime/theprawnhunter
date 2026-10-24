import contextlib
import json
import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("queue_monitor")

DEFAULT_QUEUES = ("celery", "scrape", "scanners", "validation")
DEFAULT_QUEUE_LENGTH_ALERT_THRESHOLD = 100
DEFAULT_QUEUE_OLDEST_AGE_ALERT_SECONDS = 900
DELIVERY_LATENCY_KEY = "queue_monitor:broadcast:delivery_latency_seconds"
DEFAULT_DELIVERY_LATENCY_SAMPLE_SIZE = 1000
DELIVERY_LATENCY_TTL_SECONDS = 7 * 24 * 60 * 60


def queue_tracking_key(queue_name: str) -> str:
    return f"queue_monitor:{queue_name}:enqueued"


def record_delivery_latency(
    redis_client: Any,
    latency_seconds: float,
    *,
    sample_size: int = DEFAULT_DELIVERY_LATENCY_SAMPLE_SIZE,
) -> bool:
    """Persist one created_at -> broadcasted_at latency sample in Redis."""
    if not redis_client:
        return False
    try:
        value = float(latency_seconds)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value) or value < 0:
        return False

    limit = max(1, int(sample_size))
    try:
        redis_client.lpush(DELIVERY_LATENCY_KEY, f"{value:.3f}")
        redis_client.ltrim(DELIVERY_LATENCY_KEY, 0, limit - 1)
        redis_client.expire(DELIVERY_LATENCY_KEY, DELIVERY_LATENCY_TTL_SECONDS)
        return True
    except Exception as exc:
        logger.debug("[DeliveryLatency] record failed: %s", exc)
        return False


def get_delivery_latency_summary(
    redis_client: Any,
    *,
    sample_size: int = DEFAULT_DELIVERY_LATENCY_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Return P50/P95/P99 delivery latency from the latest Redis samples."""
    empty = {
        "status": "no_data",
        "sample_size": 0,
        "p50_seconds": None,
        "p95_seconds": None,
        "p99_seconds": None,
        "mean_seconds": None,
        "max_seconds": None,
    }
    if not redis_client:
        return {**empty, "status": "unavailable", "error": "redis_unavailable"}

    limit = max(1, int(sample_size))
    try:
        raw_values = redis_client.lrange(DELIVERY_LATENCY_KEY, 0, limit - 1)
    except Exception as exc:
        logger.debug("[DeliveryLatency] read failed: %s", exc)
        return {**empty, "status": "unavailable", "error": type(exc).__name__}

    values: list[float] = []
    for raw in raw_values or []:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            values.append(value)

    if not values:
        return empty

    values.sort()
    return {
        "status": "ok",
        "sample_size": len(values),
        "p50_seconds": _percentile(values, 50),
        "p95_seconds": _percentile(values, 95),
        "p99_seconds": _percentile(values, 99),
        "mean_seconds": sum(values) / len(values),
        "max_seconds": values[-1],
    }


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Nearest-rank percentile, matching the operational latency report."""
    idx = min(
        len(sorted_values) - 1,
        max(0, int(round(percentile / 100.0 * len(sorted_values))) - 1),
    )
    return sorted_values[idx]


def record_task_enqueued(redis_client: Any, queue_name: str, task_id: str, timestamp: float | None = None) -> None:
    if not redis_client or not task_id:
        return
    queue = queue_name or "celery"
    redis_client.zadd(queue_tracking_key(queue), {task_id: timestamp or time.time()})


def record_task_started(redis_client: Any, task_id: str, queues: tuple[str, ...] = DEFAULT_QUEUES) -> None:
    if not redis_client or not task_id:
        return
    for queue in queues:
        try:
            redis_client.zrem(queue_tracking_key(queue), task_id)
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            continue


def get_queue_snapshot(redis_client: Any, queues: tuple[str, ...] = DEFAULT_QUEUES) -> dict[str, dict[str, Any]]:
    now = time.time()
    snapshot: dict[str, dict[str, Any]] = {}
    for queue in queues:
        length = 0
        oldest_enqueued_at = None
        oldest_age_seconds = None
        try:
            length = int(redis_client.llen(queue))
        except Exception:
            length = 0

        if length <= 0:
            with contextlib.suppress(Exception):
                redis_client.delete(queue_tracking_key(queue))
            snapshot[queue] = {
                "length": 0,
                "oldest_job_age_seconds": None,
                "oldest_enqueued_at": None,
            }
            continue

        try:
            oldest = redis_client.zrange(queue_tracking_key(queue), 0, 0, withscores=True)
            if oldest:
                _task_id, score = oldest[0]
                oldest_enqueued_at = datetime.fromtimestamp(float(score), tz=UTC).isoformat()
                oldest_age_seconds = max(0, int(now - float(score)))
        except Exception:
            oldest_enqueued_at = None
            oldest_age_seconds = None

        if oldest_age_seconds is None and length > 0:
            oldest_age_seconds = _best_effort_oldest_age_from_queue(redis_client, queue, now)

        snapshot[queue] = {
            "length": length,
            "oldest_job_age_seconds": oldest_age_seconds,
            "oldest_enqueued_at": oldest_enqueued_at,
        }
    return snapshot


def evaluate_queue_alerts(
    snapshot: dict[str, dict[str, Any]],
    *,
    length_threshold: int = DEFAULT_QUEUE_LENGTH_ALERT_THRESHOLD,
    oldest_age_threshold_seconds: int = DEFAULT_QUEUE_OLDEST_AGE_ALERT_SECONDS,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for queue, data in snapshot.items():
        length = int(data.get("length") or 0)
        oldest_age = data.get("oldest_job_age_seconds")
        if length_threshold > 0 and length >= length_threshold:
            alerts.append(
                {
                    "queue": queue,
                    "type": "queue_length",
                    "severity": "warning",
                    "value": length,
                    "threshold": length_threshold,
                }
            )
        if (
            oldest_age is not None
            and oldest_age_threshold_seconds > 0
            and int(oldest_age) >= oldest_age_threshold_seconds
        ):
            alerts.append(
                {
                    "queue": queue,
                    "type": "oldest_job_age",
                    "severity": "warning",
                    "value": int(oldest_age),
                    "threshold": oldest_age_threshold_seconds,
                }
            )
    return alerts


def summarize_queue_health(
    snapshot: dict[str, dict[str, Any]],
    *,
    length_threshold: int = DEFAULT_QUEUE_LENGTH_ALERT_THRESHOLD,
    oldest_age_threshold_seconds: int = DEFAULT_QUEUE_OLDEST_AGE_ALERT_SECONDS,
    delivery_latency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alerts = evaluate_queue_alerts(
        snapshot,
        length_threshold=length_threshold,
        oldest_age_threshold_seconds=oldest_age_threshold_seconds,
    )
    return {
        "status": "warning" if alerts else "healthy",
        "queues": snapshot,
        "delivery_latency": delivery_latency,
        "alerts": alerts,
        "thresholds": {
            "length": length_threshold,
            "oldest_job_age_seconds": oldest_age_threshold_seconds,
        },
    }


def _best_effort_oldest_age_from_queue(redis_client: Any, queue: str, now: float) -> int | None:
    for index in (-1, 0):
        try:
            raw = redis_client.lindex(queue, index)
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            continue
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            continue
        timestamp = _extract_timestamp(payload)
        if timestamp is not None:
            return max(0, int(now - timestamp))
    return None


def _extract_timestamp(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("created_at"),
        payload.get("timestamp"),
        (payload.get("headers") or {}).get("created_at") if isinstance(payload.get("headers"), dict) else None,
        (payload.get("headers") or {}).get("timestamp") if isinstance(payload.get("headers"), dict) else None,
        (payload.get("properties") or {}).get("timestamp") if isinstance(payload.get("properties"), dict) else None,
    ]
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
    return None
