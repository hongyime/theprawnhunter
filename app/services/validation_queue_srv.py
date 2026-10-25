"""Priority staging queue for token validation.

Scanners stage discovered candidates here instead of validating inline. Rows are
encrypted at rest (``token_ciphertext``) and deduplicated by ``token_hash`` so a
token is only validated once. The dispatcher leases the highest-priority rows;
remote outposts share the same queue, so leasing MUST be safe under concurrent
callers.

Lease atomicity (PostgREST seam)
--------------------------------
The repository talks to Postgres through PostgREST (``db.table(...)``); there is
no arbitrary-SQL seam and this task must not add migrations, so the SQL form

    UPDATE validation_queue
       SET status='leased', lease_id=..., lease_expires_at=...
     WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
     RETURNING ...

is expressed as a single guarded ``UPDATE`` with a ``status='pending'``
predicate. Postgres serialises the concurrent updates on the row lock and
re-evaluates the predicate against the committed row version; a caller that
loses the race updates (and therefore receives) zero rows. Two callers can never
be handed the same row. The candidate ``SELECT`` before it is only a hint — the
guarded ``UPDATE`` is the authority, so a lost race yields fewer rows rather
than a duplicate.

Contract consumed by later tasks (dispatcher, validate_token, outposts):

* :func:`score_candidate` — pure, deterministic, higher = validate sooner.
* :func:`enqueue` — encrypt at rest, idempotent on ``token_hash``.
* :func:`lease` — atomic claim, returns decrypted candidates.
* :func:`complete` — idempotent close-out; failures retry until
  ``VALIDATION_QUEUE_MAX_ATTEMPTS`` then drop.
* :func:`release_expired` — return expired leases to ``pending``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.database import db
from app.core.logger import get_logger
from app.core.security import security
from app.infrastructure.database import execute_async

logger = get_logger(__name__)

TABLE = "validation_queue"

# ── Scoring ──────────────────────────────────────────────────────────────────
# Trust tiers follow the plan: source-control / CI / package registries rank
# above open paste sites. Unknown sources sit between them so a novel scanner
# still validates, just behind proven provenance.
_SOURCE_TRUST: dict[str, int] = {
    "github": 100,
    "gitlab": 100,
    "bitbucket": 90,
    "ci_logs": 100,
    "npm": 95,
    "pypi": 95,
    "docker": 85,
    "huggingface": 80,
    "shodan": 60,
    "grepapp": 60,
    "fofa": 55,
    "netlas": 55,
    "urlscan": 50,
    "publicwww": 50,
    "postman": 50,
    "google": 45,
    "exa": 45,
    "pastebin": 10,
    "rentry": 10,
    "hastebin": 5,
    "ghostbin": 5,
    "dpaste": 5,
    "controlc": 5,
    "ideone": 5,
}
_DEFAULT_SOURCE_TRUST = 20

_META_BONUS = {
    "repo": 20,  # source-control provenance is strongly actionable
    "webhook_url": 15,  # a webhook hints at live infrastructure
    "chat_id": 10,  # an observed chat makes the token easier to exercise
}
_ATTEMPT_PENALTY = 30  # per previously-recorded attempt
_TOKEN_SHAPE_BONUS = 5
_BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")

# Meta keys that must never carry plaintext material; dropped on enqueue.
_FORBIDDEN_META_KEYS = frozenset({"token", "plaintext_token", "token_plaintext"})


def _source_key(source: Any) -> str:
    """Normalise a scanner source label to its trust-table key."""
    return str(source or "").strip().lower().split(":", 1)[0].strip()


def _as_attempts(value: Any) -> int:
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, attempts)


def _present(meta: Mapping[str, Any], key: str) -> bool:
    value = meta.get(key)
    return value is not None and value != ""


def score_candidate(source: str, token: str, meta: dict | None) -> int:
    """Return a deterministic priority score (higher = validate sooner).

    Pure function: no clock, no I/O, no randomness. Each signal is monotonic —
    a more trusted source never scores below a less trusted one, adding
    provenance metadata never lowers the score, and every recorded attempt
    lowers it by :data:`_ATTEMPT_PENALTY`.
    """
    details = meta if isinstance(meta, Mapping) else {}
    score = _SOURCE_TRUST.get(_source_key(source), _DEFAULT_SOURCE_TRUST)
    for key, bonus in _META_BONUS.items():
        if _present(details, key):
            score += bonus
    score -= _ATTEMPT_PENALTY * _as_attempts(details.get("attempts"))
    if _BOT_TOKEN_RE.match(str(token or "").strip()):
        score += _TOKEN_SHAPE_BONUS
    return int(score)


# ── Helpers ──────────────────────────────────────────────────────────────────
def token_hash(token: str) -> str:
    """Stable identity for a plaintext token; the only persisted token-derived value."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso_z(value: datetime) -> str:
    """UTC ISO-8601 with ``Z``; lexicographic order matches chronological order."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sanitize_meta(meta: Any) -> dict[str, Any]:
    if not isinstance(meta, Mapping):
        return {}
    return {
        key: value for key, value in meta.items() if str(key).lower() not in _FORBIDDEN_META_KEYS
    }


def _lease_filter(now: datetime) -> str:
    """PostgREST OR-filter matching unleased or expired rows."""
    return f"lease_id.is.null,lease_expires_at.lt.{_iso_z(now)}"


async def enqueue(entries: Iterable[Mapping[str, Any]], *, client: Any = db) -> int:
    """Encrypt and stage candidates; returns the number actually inserted.

    ``entries`` items are ``{"token": str, "source": str, "meta": dict | None}``.
    Insertion is idempotent on ``token_hash``: PostgREST ``ON CONFLICT DO
    NOTHING`` means a duplicate is skipped and never raises. The plaintext token
    is only ever an encryption input — it is not stored in ``meta``, a log, or
    ``token_hash``.
    """
    rows: list[dict[str, Any]] = []
    for entry in entries:
        token = str(entry.get("token") or "").strip()
        if not token:
            continue
        source = entry.get("source")
        meta = _sanitize_meta(entry.get("meta"))
        rows.append(
            {
                "token_hash": token_hash(token),
                "token_ciphertext": security.encrypt(token),
                "source": None if source is None else str(source),
                "meta": meta,
                "priority": score_candidate(str(source or ""), token, meta),
                "status": "pending",
            }
        )
    if not rows:
        return 0

    inserted = 0
    for start in range(0, len(rows), 500):
        response = await execute_async(
            client.table(TABLE).upsert(
                rows[start : start + 500],
                on_conflict="token_hash",
                ignore_duplicates=True,
            )
        )
        inserted += len(response.data or [])
    return inserted


async def lease(limit: int, seconds: int, *, client: Any = db) -> list[dict]:
    """Atomically claim up to ``limit`` candidates for ``seconds``.

    Returns decrypted ``{queue_row_id, token, source, meta, attempts, lease_id}``
    dicts. ``lease_id`` is the handle :func:`complete` expects; the other keys are
    the documented candidate contract. The returned set is disjoint from any
    concurrent :func:`lease` call — the guarded ``UPDATE`` only returns rows it
    actually transitioned to ``leased``.
    """
    if limit <= 0 or seconds <= 0:
        return []

    now = datetime.now(UTC)
    lease_id = str(uuid.uuid4())
    expires_at = _iso_z(now + timedelta(seconds=seconds))

    candidates = await execute_async(
        client.table(TABLE)
        .select("id,priority,created_at")
        .eq("status", "pending")
        .or_(_lease_filter(now))
        .order("priority", desc=True)
        .order("created_at")
        .limit(limit)
    )
    ids = [row["id"] for row in (candidates.data or []) if row.get("id")]
    if not ids:
        return []

    claimed = await execute_async(
        client.table(TABLE)
        .update(
            {
                "status": "leased",
                "lease_id": lease_id,
                "lease_expires_at": expires_at,
            }
        )
        .in_("id", ids)
        .eq("status", "pending")
        .or_(_lease_filter(now))
    )

    # Dispatch order is part of the contract; RETURNING order is unspecified.
    rows = sorted(
        claimed.data or [],
        key=lambda row: (-int(row.get("priority") or 0), str(row.get("created_at") or "")),
    )

    leased: list[dict] = []
    for row in rows:
        row_id = row.get("id")
        try:
            token = security.decrypt(row["token_ciphertext"])
        except Exception:
            # Ciphertext that no configured key can open is unrecoverable. Do
            # not log the value; drop the row so it cannot wedge the queue.
            logger.warning(f"validation_queue: dropping undecryptable row {row_id}")
            await execute_async(
                client.table(TABLE)
                .update({"status": "dropped", "lease_id": None, "lease_expires_at": None})
                .eq("id", row_id)
                .eq("lease_id", lease_id)
                .eq("status", "leased")
            )
            continue
        leased.append(
            {
                "queue_row_id": row_id,
                "token": token,
                "source": row.get("source"),
                "meta": row.get("meta") or {},
                "attempts": _as_attempts(row.get("attempts")),
                "lease_id": lease_id,
            }
        )
    return leased


async def complete(lease_id: str, results: Iterable[Mapping[str, Any]], *, client: Any = db) -> int:
    """Close out leased rows; returns how many rows transitioned.

    Each result is ``{"queue_row_id": str, "success": bool}`` (``id`` is accepted
    as an alias). Success marks the row ``done``. Failure increments
    ``attempts`` and either returns the row to ``pending`` for retry or marks it
    ``dropped`` once ``attempts`` reaches ``settings.VALIDATION_QUEUE_MAX_ATTEMPTS``.

    Idempotent: the transition is guarded by ``lease_id`` **and**
    ``status='leased'``, so replaying the same lease updates nothing and never
    double-increments ``attempts``.
    """
    if not lease_id:
        return 0

    outcomes: dict[str, bool] = {}
    for result in results:
        row_id = result.get("queue_row_id") or result.get("id")
        if not row_id:
            continue
        success = result.get("success")
        if success is None:
            success = result.get("status") == "done"
        outcomes[str(row_id)] = bool(success)
    if not outcomes:
        return 0

    leased = await execute_async(
        client.table(TABLE).select("id,attempts").eq("lease_id", lease_id).eq("status", "leased")
    )
    max_attempts = max(0, int(settings.VALIDATION_QUEUE_MAX_ATTEMPTS))

    completed = 0
    for row in leased.data or []:
        row_id = str(row.get("id"))
        if row_id not in outcomes:
            continue
        attempts = _as_attempts(row.get("attempts")) + 1
        if outcomes[row_id]:
            status = "done"
        elif attempts >= max_attempts:
            status = "dropped"
        else:
            status = "pending"
        updated = await execute_async(
            client.table(TABLE)
            .update(
                {
                    "status": status,
                    "attempts": attempts,
                    "lease_id": None,
                    "lease_expires_at": None,
                }
            )
            .eq("id", row.get("id"))
            .eq("lease_id", lease_id)
            .eq("status", "leased")
        )
        if updated.data:
            completed += 1
    return completed


async def release_expired(*, client: Any = db) -> int:
    """Return expired leases to ``pending``; ``attempts`` is left intact."""
    now = _iso_z(datetime.now(UTC))
    response = await execute_async(
        client.table(TABLE)
        .update({"status": "pending", "lease_id": None, "lease_expires_at": None})
        .eq("status", "leased")
        .lt("lease_expires_at", now)
    )
    return len(response.data or [])


__all__ = [
    "complete",
    "enqueue",
    "lease",
    "release_expired",
    "score_candidate",
    "token_hash",
]
