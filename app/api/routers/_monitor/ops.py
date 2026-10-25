"""Monitor operations routes — pause / resume / status parity with the bot.

The bot's ``/pause`` writes ``system:paused`` and ``/resume`` deletes it; every
scanner, broadcaster and validator only checks that key. These routes reuse the
exact same Redis key (imported, never copied) so the dashboard cannot drift from
the bot semantics. No new storage, no second pause mechanism.
"""

from pydantic import BaseModel

from app.core.queue_monitor import DEFAULT_QUEUES, get_queue_snapshot
from app.core.redis_srv import redis_srv
from app.services._bot_listener.runtime import PAUSE_KEY

from .common import *

router = APIRouter()

# Audit event names for dashboard-initiated changes. Kept local so this lane
# does not widen the shared AuditEvent surface used by unrelated services.
OPS_PAUSE_EVENT = "monitor.ops.pause"
OPS_RESUME_EVENT = "monitor.ops.resume"
OPS_ACTOR = "monitor-api"

# Cluster-wide view of live bot pollers, same namespace the listener uses.
_BOT_LOCK_PATTERN = "bot_listener:poll_lock:*"


class QueueLengths(BaseModel):
    """Depth of each Celery queue the dashboard exposes."""

    celery: int = 0
    scrape: int = 0
    scanners: int = 0
    validation: int = 0


class OpsStatusOut(BaseModel):
    """Pause state plus the health fields the bot's /status command reports."""

    paused: bool
    redis: Literal["ok", "unreachable"]
    pending_broadcasts: int | None
    queue_lengths: QueueLengths
    bot_count: int | None


class OpsToggleOut(BaseModel):
    paused: bool


def _redis_client():
    """Return the shared sync Redis client (calls are moved off the loop)."""
    return redis_srv.client


def _empty_queue_lengths() -> dict[str, int]:
    return {queue: 0 for queue in DEFAULT_QUEUES}


def _read_redis_state() -> dict[str, Any]:
    """Collect pause state, queue depth and live bot count in one thread hop.

    Every branch degrades to a stable value: a Redis outage must surface as
    ``redis: "unreachable"`` with HTTP 200, never as an unhandled 500.
    """
    try:
        client = _redis_client()
        client.ping()
    except Exception as exc:
        logger.warning("monitor/ops status: Redis unreachable (%s)", type(exc).__name__)
        return {
            "redis": "unreachable",
            "paused": False,
            "queue_lengths": _empty_queue_lengths(),
            "bot_count": None,
        }

    try:
        paused = bool(client.get(PAUSE_KEY))
    except Exception as exc:
        logger.warning("monitor/ops status: pause key read failed (%s)", type(exc).__name__)
        paused = False

    try:
        snapshot = get_queue_snapshot(client)
        queue_lengths = {
            queue: int((snapshot.get(queue) or {}).get("length") or 0)
            for queue in DEFAULT_QUEUES
        }
    except Exception as exc:
        logger.warning("monitor/ops status: queue probe failed (%s)", type(exc).__name__)
        queue_lengths = _empty_queue_lengths()

    try:
        locked_bots = sum(
            1 for _ in client.scan_iter(match=_BOT_LOCK_PATTERN, count=100)
        )
        # The API process never holds bot locks; zero means "not observable here",
        # not "no bots", so report unknown instead of a misleading 0.
        bot_count: int | None = locked_bots or None
    except Exception as exc:
        logger.warning("monitor/ops status: bot lock scan failed (%s)", type(exc).__name__)
        bot_count = None

    return {
        "redis": "ok",
        "paused": paused,
        "queue_lengths": queue_lengths,
        "bot_count": bot_count,
    }


async def _pending_broadcast_count() -> int | None:
    """Count messages awaiting broadcast using the shared exact-count helper."""
    try:
        return await _exact_count_async(
            "exfiltrated_messages", "is_broadcasted", False
        )
    except Exception as exc:
        logger.warning(
            "monitor/ops status: pending broadcast count failed (%s)",
            type(exc).__name__,
        )
        return None


def _set_pause_key() -> bool:
    try:
        _redis_client().set(PAUSE_KEY, "true")
        return True
    except Exception as exc:
        logger.warning("monitor/ops pause: Redis write failed (%s)", type(exc).__name__)
        return False


def _delete_pause_key() -> bool:
    try:
        _redis_client().delete(PAUSE_KEY)
        return True
    except Exception as exc:
        logger.warning("monitor/ops resume: Redis delete failed (%s)", type(exc).__name__)
        return False


def _audit(event_type: str, paused: bool) -> None:
    """Best-effort audit line; never let logging break a state change."""
    try:
        AuditLogger.log(
            event_type,
            user=OPS_ACTOR,
            details={"paused": paused},
        )
    except Exception as exc:
        logger.warning("monitor/ops audit failed (%s)", type(exc).__name__)


@router.get(
    "/ops/status",
    response_model=OpsStatusOut,
    dependencies=[Depends(require_monitor_key)],
)
async def ops_status():
    """Return pause state, Redis health, pending broadcasts and queue depths."""
    state = await asyncio.to_thread(_read_redis_state)
    pending_broadcasts = await _pending_broadcast_count()
    return OpsStatusOut(
        paused=state["paused"],
        redis=state["redis"],
        pending_broadcasts=pending_broadcasts,
        queue_lengths=QueueLengths(**state["queue_lengths"]),
        bot_count=state["bot_count"],
    )


@router.post(
    "/ops/pause",
    response_model=OpsToggleOut,
    dependencies=[Depends(require_monitor_key)],
)
async def ops_pause():
    """Set the same ``system:paused`` key the bot's /pause writes."""
    if not await asyncio.to_thread(_set_pause_key):
        raise HTTPException(status_code=503, detail="Redis unavailable")
    _audit(OPS_PAUSE_EVENT, paused=True)
    return OpsToggleOut(paused=True)


@router.post(
    "/ops/resume",
    response_model=OpsToggleOut,
    dependencies=[Depends(require_monitor_key)],
)
async def ops_resume():
    """Delete ``system:paused`` — identical to the bot's /resume semantics."""
    if not await asyncio.to_thread(_delete_pause_key):
        raise HTTPException(status_code=503, detail="Redis unavailable")
    _audit(OPS_RESUME_EVENT, paused=False)
    return OpsToggleOut(paused=False)
