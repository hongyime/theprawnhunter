"""
Health check router for monitoring system status.
Provides endpoints to check database, Redis, and service health.
"""
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.audit import AuditEvent
from app.core.auth import require_monitor_key
from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/health", tags=["Health"])


def _parse_db_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _recent_audit_rows(event_type: str, since_iso: str, limit: int = 500) -> list[dict[str, Any]]:
    from app.core.database import db

    response = (
        db.table("audit_logs")
        .select("event_type, success, details, timestamp")
        .eq("event_type", event_type)
        .gte("timestamp", since_iso)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


def _latest_audit_row(event_type: str) -> dict[str, Any] | None:
    from app.core.database import db

    response = (
        db.table("audit_logs")
        .select("event_type, success, details, timestamp")
        .eq("event_type", event_type)
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def _canary_status(now: datetime) -> dict[str, Any]:
    if not settings.CANARY_CREDENTIAL_ID:
        return {"status": "disabled", "reason": "CANARY_CREDENTIAL_ID not configured"}

    try:
        latest = _latest_audit_row(AuditEvent.CANARY_FLOW_CHECK)
    except Exception as exc:
        logger.warning("[OperationalHealth] Canary audit query failed: %s", exc)
        return {"status": "unknown", "error": "audit_query_failed"}

    if not latest:
        return {"status": "failed", "reason": "no_canary_audit_rows"}

    timestamp = _parse_db_timestamp(latest.get("timestamp"))
    age_seconds = int((now - timestamp).total_seconds()) if timestamp else None
    details = latest.get("details") if isinstance(latest.get("details"), dict) else {}
    status = "healthy" if latest.get("success") else "failed"
    if age_seconds is None:
        status = "unknown"
    elif age_seconds > settings.CANARY_STALE_SECONDS:
        status = "stale"

    return {
        "status": status,
        "last_success": bool(latest.get("success")),
        "last_checked_at": latest.get("timestamp"),
        "age_seconds": age_seconds,
        "stale_after_seconds": settings.CANARY_STALE_SECONDS,
        "details": details,
    }


def _failure_summary(now: datetime) -> dict[str, Any]:
    window_hours = max(1, int(settings.OPERATIONAL_REPORT_WINDOW_HOURS))
    since = now - timedelta(hours=window_hours)
    since_iso = since.isoformat()
    result: dict[str, Any] = {
        "window_hours": window_hours,
        "broadcast_failures": {
            "total": 0,
            "by_reason": {},
            "threshold": settings.BROADCAST_FAILURE_ALERT_THRESHOLD,
            "alert": False,
        },
        "scrape_terminal_reasons": {
            "total": 0,
            "by_reason": {},
            "threshold": settings.SCRAPE_REASON_ALERT_THRESHOLD,
            "alert": False,
        },
    }

    try:
        rows = _recent_audit_rows(AuditEvent.BROADCAST_FAILED, since_iso)
        by_reason = Counter(
            (row.get("details") or {}).get("reason", "unknown")
            for row in rows
            if isinstance(row.get("details"), dict)
        )
        result["broadcast_failures"] = {
            "total": len(rows),
            "by_reason": dict(by_reason),
            "threshold": settings.BROADCAST_FAILURE_ALERT_THRESHOLD,
            "alert": any(
                count >= settings.BROADCAST_FAILURE_ALERT_THRESHOLD
                for count in by_reason.values()
            ),
        }
    except Exception as exc:
        logger.warning("[OperationalHealth] Broadcast failure query failed: %s", exc)
        result["broadcast_failures"]["error"] = "audit_query_failed"

    try:
        rows = _recent_audit_rows(AuditEvent.SCRAPE_CLASSIFIED, since_iso)
        terminal_rows = []
        for row in rows:
            details = row.get("details") if isinstance(row.get("details"), dict) else {}
            reason = details.get("reason")
            if reason and reason not in {"success", "no_new_messages"}:
                terminal_rows.append(row)
        by_reason = Counter(
            (row.get("details") or {}).get("reason", "unknown")
            for row in terminal_rows
            if isinstance(row.get("details"), dict)
        )
        result["scrape_terminal_reasons"] = {
            "total": len(terminal_rows),
            "by_reason": dict(by_reason),
            "threshold": settings.SCRAPE_REASON_ALERT_THRESHOLD,
            "alert": any(
                count >= settings.SCRAPE_REASON_ALERT_THRESHOLD
                for count in by_reason.values()
            ),
        }
    except Exception as exc:
        logger.warning("[OperationalHealth] Scrape classification query failed: %s", exc)
        result["scrape_terminal_reasons"]["error"] = "audit_query_failed"

    return result


@router.get("/")
async def health_check():
    """
    Basic health check endpoint.
    Returns 200 if API is responsive.
    """
    return {"status": "healthy", "service": "telegram-hunter-api"}


@router.get("/detailed", dependencies=[Depends(require_monitor_key)])
async def detailed_health():
    """
    Detailed health check with dependency status (protected by X-Monitor-Key).
    """
    health_status = {
        "status": "healthy",
        "checks": {}
    }

    # Check Database
    try:
        from app.core.db_retry import DatabaseHealth
        DatabaseHealth.check_connection()
        health_status["checks"]["database"] = {"status": "healthy"}
    except Exception as e:
        health_status["checks"]["database"] = {"status": "unhealthy", "error": str(e)}
        health_status["status"] = "degraded"

    # Check Redis
    try:
        import redis
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        client.ping()
        health_status["checks"]["redis"] = {"status": "healthy"}
    except Exception as e:
        health_status["checks"]["redis"] = {"status": "unhealthy", "error": str(e)}
        health_status["status"] = "degraded"

    # Check Telegram Bot API
    try:
        import httpx
        token = settings.bot_tokens[0]
        # Mask token in URL — only pass the bot_id prefix for logging safety
        url = f"https://api.telegram.org/bot{token}/getMe"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        if response.status_code == 200:
            health_status["checks"]["telegram_bot"] = {"status": "healthy"}
        else:
            health_status["checks"]["telegram_bot"] = {"status": "unhealthy", "error": "API unreachable"}
            health_status["status"] = "degraded"
    except Exception:
        # Do NOT include the exception string — it may contain the bot token in a URL
        health_status["checks"]["telegram_bot"] = {"status": "unhealthy", "error": "connection_failed"}
        health_status["status"] = "degraded"

    # Return 503 if any critical service is down
    if health_status["status"] == "degraded":
        raise HTTPException(status_code=503, detail=health_status)

    return health_status


@router.get("/metrics", dependencies=[Depends(require_monitor_key)])
async def get_metrics():
    """
    Get system metrics (protected by X-Monitor-Key).
    """
    from app.core.metrics import metrics

    return {
        "summary": metrics.get_summary(),
        "metrics": metrics.get_all_metrics()
    }


@router.get("/queues", dependencies=[Depends(require_monitor_key)])
async def get_queue_health():
    """
    Get operational queue depth and oldest tracked job age.
    """
    try:
        import redis

        from app.core.queue_monitor import get_queue_snapshot, summarize_queue_health

        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        snapshot = get_queue_snapshot(client)
        return summarize_queue_health(
            snapshot,
            length_threshold=settings.QUEUE_ALERT_LENGTH_THRESHOLD,
            oldest_age_threshold_seconds=settings.QUEUE_ALERT_OLDEST_AGE_SECONDS,
        )
    except Exception as e:
        logger.exception("queue health failed")
        raise HTTPException(
            status_code=503,
            detail={"status": "degraded", "error": "unavailable"},
        ) from e


@router.get("/operational", dependencies=[Depends(require_monitor_key)])
async def get_operational_health():
    """Operational readiness report: health, queues, canary, and recent failures."""
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "status": "healthy",
        "generated_at": now.isoformat(),
    }

    try:
        import redis

        from app.core.queue_monitor import get_queue_snapshot, summarize_queue_health

        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        queue_summary = summarize_queue_health(
            get_queue_snapshot(client),
            length_threshold=settings.QUEUE_ALERT_LENGTH_THRESHOLD,
            oldest_age_threshold_seconds=settings.QUEUE_ALERT_OLDEST_AGE_SECONDS,
        )
    except Exception as exc:
        logger.warning("[OperationalHealth] Queue probe failed: %s", exc)
        queue_summary = {"status": "unknown", "error": "queue_probe_failed"}

    canary = _canary_status(now)
    failures = _failure_summary(now)

    report["queues"] = queue_summary
    report["canary"] = canary
    report["failures"] = failures

    degraded = [
        queue_summary.get("status") not in {"healthy"},
        canary.get("status") in {"failed", "stale"},
        bool(failures.get("broadcast_failures", {}).get("alert")),
        bool(failures.get("scrape_terminal_reasons", {}).get("alert")),
    ]
    if any(degraded):
        report["status"] = "degraded"

    return report


@router.get("/circuit-breakers", dependencies=[Depends(require_monitor_key)])
async def get_circuit_breakers():
    """
    Get circuit breaker status (protected by X-Monitor-Key).
    """
    from app.core.circuit_breaker import get_all_circuit_status

    return {
        "circuit_breakers": get_all_circuit_status()
    }


@router.post(
    "/circuit-breakers/{service}/reset",
    dependencies=[Depends(require_monitor_key)],
)
async def reset_circuit_breaker(service: str):
    """
    Manually reset a circuit breaker (protected by X-Monitor-Key).
    Use this to force-enable a service after fixing issues.
    """
    from app.core.circuit_breaker import get_circuit_breaker

    try:
        breaker = get_circuit_breaker(service)
        breaker.reset()
        return {"status": "success", "message": f"Circuit breaker for {service} reset"}
    except Exception as e:
        logger.exception("circuit breaker reset failed")
        raise HTTPException(status_code=400, detail="reset failed") from e


# ==============================================================================
# OBSERVABILITY — daily quota usage and bot pool state
# ==============================================================================

# Hardcoded daily budget per scanner. Populate via env if these ever need to
# vary per deployment; today they're stable across environments.
_QUOTA_LIMITS: dict[str, int] = {
    "shodan": 100,
    "netlas": 50,
    "github": 5000,
    "urlscan": 100,
}


@router.get("/quotas", dependencies=[Depends(require_monitor_key)])
async def get_quotas():
    """Per-service daily API-budget usage.

    Reads Redis counters at ``quota:{service}:{yyyymmdd}`` (UTC date) for
    each supported scanner and returns ``{service: {used_today, limit,
    pct}}``. Services whose Redis counter is absent report ``used_today=0``.
    ``limit`` is null when no budget is known for a service.
    """
    from datetime import datetime

    import redis

    today_key = datetime.now(UTC).strftime("%Y%m%d")
    try:
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        out: dict[str, dict] = {}
        for service, limit in _QUOTA_LIMITS.items():
            raw = client.get(f"quota:{service}:{today_key}")
            try:
                used_today = int(raw) if raw is not None else 0
            except (ValueError, TypeError):
                used_today = 0
            pct = round(100.0 * used_today / limit, 2) if limit else None
            out[service] = {
                "used_today": used_today,
                "limit": limit,
                "pct": pct,
            }
        return {"date_utc": today_key, "quotas": out}
    except Exception as e:
        logger.exception("quota probe failed")
        raise HTTPException(
            status_code=503,
            detail={"status": "degraded", "error": "unavailable"},
        ) from e


@router.get("/bot-pool", dependencies=[Depends(require_monitor_key)])
async def get_bot_pool():
    """Bot pool state — total configured, active bots (cluster-wide), and Redis lock view.

    ``total_bots`` counts tokens configured via ``MONITOR_BOT_TOKEN``.

    ``active_bots`` counts DISTINCT bot IDs currently holding a Redis poll
    lock (``bot_listener:poll_lock:{bot_id}``). This is the cross-process,
    cluster-wide view: any worker/listener actively polling a bot renews
    its lock, so a live count here reflects real cluster activity.

    ``locked_bots`` is a synonym for ``active_bots`` kept for backward
    compatibility with existing dashboards.

    ``local_cached_clients`` counts connected Telethon clients cached in
    the API process's ``BotClientManager`` — near-zero in typical deploys
    because the API rarely opens Telethon sessions. Useful only for
    debugging in-process client warmth.

    ``oldest_lock_age_seconds`` is a *time-since-last-renew* proxy:
    ``LOCK_TTL_SECONDS − min(TTL)`` across all lock keys. A large value
    means a lock is nearing expiry (poller may have stopped renewing).
    """
    from app.core.constants import LOCK_TTL_SECONDS
    from app.services.bot_manager_srv import bot_manager

    total_bots = len(settings.bot_tokens)

    # Local cache (kept for debugging only — do not use as cluster signal)
    local_cached_clients = 0
    for _token, client in list(bot_manager._clients.items()):
        try:
            if client.is_connected():
                local_cached_clients += 1
        except Exception:
            continue

    # Cross-process signal — Redis lock keys are the source of truth
    active_bots = 0
    oldest_lock_age_seconds: int | None = None
    try:
        import redis
        rc = redis.from_url(settings.REDIS_URL, decode_responses=True)
        lock_keys = list(rc.scan_iter(match="bot_listener:poll_lock:*", count=100))
        active_bots = len(lock_keys)
        if lock_keys:
            ttls: list[int] = []
            for key in lock_keys:
                ttl = rc.ttl(key)
                # -1 = no expire (unexpected here), -2 = missing (raced). Skip both.
                if isinstance(ttl, int) and ttl >= 0:
                    ttls.append(ttl)
            if ttls:
                # Smallest TTL = oldest (nearest to expiry) = largest elapsed since renew.
                oldest_lock_age_seconds = max(0, LOCK_TTL_SECONDS - min(ttls))
    except Exception as e:
        logger.warning(f"[BotPool] Redis lock enumeration failed: {e}")

    return {
        "total_bots": total_bots,
        "active_bots": active_bots,
        "locked_bots": active_bots,  # alias for backward compat
        "local_cached_clients": local_cached_clients,
        "oldest_lock_age_seconds": oldest_lock_age_seconds,
        "lock_ttl_seconds": LOCK_TTL_SECONDS,
    }
