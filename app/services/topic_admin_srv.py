import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.core.audit import AuditEvent, AuditLogger
from app.core.config import settings
from app.core.database import db

logger = logging.getLogger(__name__)


async def async_execute(query_builder):
    """Executes a Supabase query builder synchronously in a background thread."""
    return await asyncio.to_thread(query_builder.execute)


def coerce_topic_id(meta: dict[str, Any]) -> int | None:
    topic_id = meta.get("topic_id") if isinstance(meta, dict) else None
    try:
        topic_id_int = int(topic_id)
    except (TypeError, ValueError):
        return None
    if topic_id_int <= 1:
        return None
    return topic_id_int


async def fetch_revoked_topic_candidates(limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    page_size = min(max(limit * 4, 25), 500)
    offset = 0
    canary_credential_id = getattr(settings, "CANARY_CREDENTIAL_ID", None)
    seen_topic_ids: set[int] = set()

    while len(candidates) < limit and offset < 5000:
        response = await async_execute(
            db.table("discovered_credentials")
            .select("id, status, meta, updated_at")
            .eq("status", "revoked")
            .order("updated_at", desc=True)
            .range(offset, offset + page_size - 1)
        )
        rows = response.data or []
        if not rows:
            break

        for row in rows:
            if canary_credential_id and row.get("id") == canary_credential_id:
                continue
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            topic_id = coerce_topic_id(meta)
            if not topic_id or meta.get("topic_closed_at"):
                continue
            if topic_id in seen_topic_ids:
                continue
            seen_topic_ids.add(topic_id)
            candidates.append(
                {
                    "id": row.get("id"),
                    "topic_id": topic_id,
                    "meta": meta,
                    "updated_at": row.get("updated_at"),
                }
            )
            if len(candidates) >= limit:
                break

        if len(rows) < page_size:
            break
        offset += page_size

    return candidates


async def fetch_revoked_topic_candidate(credential_id: str) -> list[dict[str, Any]]:
    response = await async_execute(
        db.table("discovered_credentials")
        .select("id, status, meta, updated_at")
        .eq("id", credential_id)
        .eq("status", "revoked")
        .limit(1)
    )
    rows = response.data or []
    if not rows:
        return []

    row = rows[0]
    if getattr(settings, "CANARY_CREDENTIAL_ID", None) and row.get("id") == settings.CANARY_CREDENTIAL_ID:
        return []
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    topic_id = coerce_topic_id(meta)
    if not topic_id or meta.get("topic_closed_at"):
        return []
    return [
        {
            "id": row.get("id"),
            "topic_id": topic_id,
            "meta": meta,
            "updated_at": row.get("updated_at"),
        }
    ]


def get_broadcaster():
    from app.services.broadcaster_srv import BroadcasterService

    return BroadcasterService()


async def close_revoked_topics_logic(
    limit: int = 50,
    dry_run: bool = True,
    credential_id: str | None = None,
    *,
    actor: str = "celery_worker",
    force: bool = False,
) -> dict:
    limit = max(1, min(int(limit or getattr(settings, "REVOKED_TOPIC_CLOSE_BATCH_SIZE", 25)), 500))
    dry_run = bool(dry_run)
    if not dry_run and not force and not getattr(settings, "AUTO_CLOSE_REVOKED_TOPICS", True):
        return {
            "status": "disabled",
            "dry_run": False,
            "candidate_count": 0,
            "closed": 0,
            "failed": 0,
            "topics": [],
        }

    if credential_id:
        candidates = await fetch_revoked_topic_candidate(credential_id)
    else:
        candidates = await fetch_revoked_topic_candidates(limit)
    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "dry_run": dry_run,
        "credential_id": credential_id,
        "candidate_count": len(candidates),
        "closed": 0,
        "failed": 0,
        "topics": [],
    }

    if dry_run:
        result["topics"] = [
            {
                "credential_id": row["id"],
                "topic_id": row["topic_id"],
                "action": "would_close",
            }
            for row in candidates
        ]
        return result

    broadcaster = get_broadcaster()
    closed_at = datetime.now(UTC).isoformat()
    monitor_group_id = getattr(settings, "MONITOR_GROUP_ID", None)
    delay_seconds = max(0.0, float(getattr(settings, "REVOKED_TOPIC_CLOSE_DELAY_SECONDS", 0.5) or 0.0))
    timeout_seconds = float(getattr(settings, "REVOKED_TOPIC_CLOSE_TIMEOUT_SECONDS", 10.0) or 10.0)
    if timeout_seconds <= 0:
        timeout_seconds = 10.0

    for row in candidates:
        credential_id = row["id"]
        topic_id = row["topic_id"]
        try:
            ok = await asyncio.wait_for(
                broadcaster.close_topic(monitor_group_id, topic_id),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            logger.warning(f"Topic close timed out for {topic_id}")
            ok = False
        entry = {
            "credential_id": credential_id,
            "topic_id": topic_id,
            "closed": bool(ok),
        }
        if ok:
            fresh = await async_execute(
                db.table("discovered_credentials")
                .select("meta")
                .eq("id", credential_id)
                .limit(1)
            )
            fresh_rows = fresh.data or []
            current_meta = (
                fresh_rows[0].get("meta")
                if fresh_rows and isinstance(fresh_rows[0].get("meta"), dict)
                else row.get("meta", {})
            )
            meta = dict(current_meta or {})
            meta["topic_closed_at"] = closed_at
            meta["topic_closed_reason"] = "credential_revoked"
            meta["topic_status"] = "closed"
            await async_execute(
                db.table("discovered_credentials")
                .update({"meta": meta})
                .eq("id", credential_id)
            )
            result["closed"] += 1
        else:
            result["failed"] += 1
        result["topics"].append(entry)
        if delay_seconds:
            await asyncio.sleep(delay_seconds)

    AuditLogger.log(
        AuditEvent.TOPIC_CLOSED,
        user=actor,
        details={
            "reason": "credential_revoked",
            "candidate_count": result["candidate_count"],
            "closed": result["closed"],
            "failed": result["failed"],
            "dry_run": False,
            "credential_ids": [row["credential_id"] for row in result["topics"][:20]],
            "truncated": len(result["topics"]) > 20,
        },
        success=result["failed"] == 0,
    )

    if result["failed"]:
        result["status"] = "partial"
    return result
