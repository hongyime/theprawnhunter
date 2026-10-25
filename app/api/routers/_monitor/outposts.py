"""Monitor outpost lease/result routes (disabled by default).

Remote outposts pull a batch of priority-ordered validation candidates from
``POST /monitor/outposts/lease``, run Telegram ``getMe`` locally (one IP per
outpost), then report each outcome to ``POST /monitor/outposts/result``.

``OUTPOST_ENABLED`` ships ``False`` and is checked before any queue access, so
with the shipped configuration both endpoints are inert (403) and the queue is
never touched. Operators opt in explicitly after provisioning a host.

Lease safety is owned by :mod:`app.services.validation_queue_srv`: ``lease``
atomically claims rows (guarded UPDATE) and ``complete`` is guarded by
``lease_id`` + ``status='leased'``, so a replayed result never double-increments
``attempts``. This module adds the HTTP contract on top:

* lease returns decrypted candidates and the shared lease id / expiry;
* result validates the lease against the live row before calling ``complete``;
* an already-closed row is acknowledged as a ``duplicate`` no-op (200);
* an unknown/expired/mismatched lease is refused with 409 and no state change.

Plaintext tokens are never logged here — only returned in the lease response.
"""

import logging
from datetime import UTC, datetime, timedelta

from app.core.config import outposts_enabled, settings
from app.schemas.models import (
    OutpostCandidateOut,
    OutpostLeaseOut,
    OutpostResultIn,
    OutpostResultOut,
)
from app.services import validation_queue_srv

from .common import Any, APIRouter, HTTPException, db, execute_async

logger = logging.getLogger(__name__)

router = APIRouter()

# Statuses that mean "this row was already closed out". A result for one of
# these is a replay, not a lease violation.
_CLOSED_STATUSES = frozenset({"done", "dropped"})


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _active_lease_ids() -> set[str]:
    """Distinct lease ids currently holding rows (expired leases excluded).

    PostgREST compares ``timestamp`` columns lexicographically in the filter
    DSL, but fetching the rows and comparing here keeps the cap check simple
    and test-double friendly; the leased working set is bounded by the
    dispatcher batch and outpost lease sizes.
    """
    response = await execute_async(
        db.table("validation_queue").select("lease_id,lease_expires_at").eq("status", "leased")
    )
    now = datetime.now(UTC)
    active: set[str] = set()
    for row in response.data or []:
        lease_id = row.get("lease_id")
        if not lease_id:
            continue
        expires_at = _parse_ts(row.get("lease_expires_at"))
        if expires_at is None or expires_at > now:
            active.add(str(lease_id))
    return active


async def _fetch_queue_row(row_id: str) -> dict[str, Any] | None:
    response = await execute_async(
        db.table("validation_queue")
        .select("id,status,lease_id,lease_expires_at,attempts")
        .eq("id", row_id)
        .limit(1)
    )
    rows = response.data or []
    return rows[0] if rows else None


@router.post("/outposts/lease", response_model=OutpostLeaseOut)
async def lease_outpost_candidates():
    """Lease a priority-ordered batch of decrypted candidates.

    Requires ``X-Monitor-Key`` (enforced by the parent monitor router).
    Returns 403 when ``OUTPOST_ENABLED`` is False — before any DB call.

    ``OUTPOST_MAX_ACTIVE`` caps concurrent leases (distinct live lease ids).
    429 is returned when the cap is already reached; ``0`` means no cap.
    """
    if not outposts_enabled():
        raise HTTPException(status_code=403, detail="outposts disabled")

    max_active = int(settings.OUTPOST_MAX_ACTIVE)
    if max_active > 0:
        active = await _active_lease_ids()
        if len(active) >= max_active:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"outpost lease cap reached ({max_active} active); "
                    "wait for an outpost lease to expire or report its results"
                ),
            )

    rows = await validation_queue_srv.lease(
        settings.OUTPOST_LEASE_SIZE,
        settings.OUTPOST_LEASE_SECONDS,
        client=db,
    )
    if not rows:
        return OutpostLeaseOut()

    # ``lease`` stamps every claimed row with one generated lease id; derive it
    # once (a test pins that all rows share it) and expire at the configured
    # window, matching the lease_expires_at the service wrote.
    lease_id = str(rows[0].get("lease_id") or "")
    if not lease_id:
        logger.error("outpost lease returned rows without a lease_id")
        raise HTTPException(status_code=500, detail="Internal error")

    expires_at = datetime.now(UTC) + timedelta(seconds=int(settings.OUTPOST_LEASE_SECONDS))
    candidates = [
        OutpostCandidateOut(
            queue_row_id=str(row.get("queue_row_id")),
            token=str(row.get("token") or ""),
            source=None if row.get("source") is None else str(row["source"]),
            meta=row.get("meta") if isinstance(row.get("meta"), dict) else {},
        )
        for row in rows
        if row.get("queue_row_id")
    ]
    return OutpostLeaseOut(
        lease_id=lease_id,
        expires_at=expires_at,
        candidates=candidates,
    )


@router.post("/outposts/result", response_model=OutpostResultOut)
async def report_outpost_result(payload: OutpostResultIn):
    """Record one outpost validation outcome.

    Status codes:
    - 403 ``outposts disabled`` (and before any DB call);
    - 409 unknown lease / queue row, expired lease, or a lease that does not
      currently own the row — no state change;
    - 200 ``completed`` after ``complete`` transitions the row;
    - 200 ``duplicate`` when the same ``(lease_id, queue_row_id)`` is replayed
      (already ``done``/``dropped``, or a failed row returned to ``pending``
      by an earlier report) — no second ``attempts`` increment.
    """
    if not outposts_enabled():
        raise HTTPException(status_code=403, detail="outposts disabled")

    row = await _fetch_queue_row(payload.queue_row_id)
    if row is None:
        raise HTTPException(status_code=409, detail="unknown lease or queue row")

    status = str(row.get("status") or "")
    row_lease_id = row.get("lease_id")
    attempts = int(row.get("attempts") or 0)

    if status == "leased" and str(row_lease_id or "") == payload.lease_id:
        expires_at = _parse_ts(row.get("lease_expires_at"))
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise HTTPException(status_code=409, detail="lease expired; request a new lease")
        await validation_queue_srv.complete(
            payload.lease_id,
            [{"queue_row_id": payload.queue_row_id, "success": payload.ok}],
            client=db,
        )
        return OutpostResultOut(
            lease_id=payload.lease_id,
            queue_row_id=payload.queue_row_id,
            status="completed",
        )

    if status == "leased":
        # Live row owned by a different lease: never let a stale/forged lease
        # touch it.
        raise HTTPException(status_code=409, detail="unknown or inactive lease")

    if status in _CLOSED_STATUSES or (status == "pending" and attempts > 0):
        # Replay of an already-closed result; idempotent no-op.
        return OutpostResultOut(
            lease_id=payload.lease_id,
            queue_row_id=payload.queue_row_id,
            status="duplicate",
        )

    raise HTTPException(status_code=409, detail="unknown or inactive lease")
