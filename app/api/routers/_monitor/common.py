import asyncio

import csv

import io

import logging

from datetime import UTC, datetime

from typing import Any, Literal

from urllib.parse import urlsplit, urlunsplit

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from fastapi.responses import JSONResponse, StreamingResponse

from app.core.audit import AuditEvent, AuditLogger

from app.core.auth import require_monitor_key

from app.core.database import db

from app.infrastructure.database import execute_async

from app.schemas.models import (
    CredentialOut,
    EngagementLifecycleIn,
    FindingDetailOut,
    FindingEvidenceOut,
    FindingFeedbackIn,
    FindingFeedbackOut,
    FindingOut,
    FindingStatus,
    FindingType,
    MessageOut,
    StatsOut,
)

logger = logging.getLogger(__name__)

_STATS_CACHE_TTL_SECONDS = 30

_STATS_REDIS_KEY = "monitor:stats:cached"

def _get_monitor_stats() -> StatsOut:
    try:
        stats = _get_monitor_stats_from_rpc()
    except Exception as exc:
        if not _is_missing_monitor_stats_rpc(exc):
            raise
        logger.warning("monitor stats RPC unavailable; falling back to exact narrow counts")
        stats = None

    if stats is not None:
        return stats

    return StatsOut(
        credentials_total=_exact_count("discovered_credentials"),
        credentials_active=_exact_count("discovered_credentials", "status", "active"),
        messages_exfiltrated=_exact_count("exfiltrated_messages"),
        messages_broadcasted=_exact_count("exfiltrated_messages", "is_broadcasted", True),
    )

async def _get_monitor_stats_async() -> StatsOut:
    try:
        stats = await _get_monitor_stats_from_rpc_async()
    except Exception as exc:
        if not _is_missing_monitor_stats_rpc(exc):
            raise
        logger.warning("monitor stats RPC unavailable; falling back to exact narrow counts")
        stats = None

    if stats is not None:
        return stats

    credentials_total = await _exact_count_async("discovered_credentials")
    credentials_active = await _exact_count_async(
        "discovered_credentials", "status", "active"
    )
    messages_exfiltrated = await _exact_count_async("exfiltrated_messages")
    messages_broadcasted = await _exact_count_async(
        "exfiltrated_messages", "is_broadcasted", True
    )
    return StatsOut(
        credentials_total=credentials_total,
        credentials_active=credentials_active,
        messages_exfiltrated=messages_exfiltrated,
        messages_broadcasted=messages_broadcasted,
    )

def _get_monitor_stats_from_rpc() -> StatsOut | None:
    res = db.rpc("get_monitor_stats").execute()
    return _monitor_stats_from_result(res)

async def _get_monitor_stats_from_rpc_async() -> StatsOut | None:
    res = await execute_async(db.rpc("get_monitor_stats"))
    return _monitor_stats_from_result(res)

def _monitor_stats_from_result(res: Any) -> StatsOut | None:
    rows = res.data or []
    row = rows[0] if isinstance(rows, list) and rows else rows if isinstance(rows, dict) else None
    if not row:
        return None
    return StatsOut(
        credentials_total=int(row.get("credentials_total") or 0),
        credentials_active=int(row.get("credentials_active") or 0),
        messages_exfiltrated=int(row.get("messages_exfiltrated") or 0),
        messages_broadcasted=int(row.get("messages_broadcasted") or 0),
    )

def _exact_count(table: str, column: str | None = None, value: Any = None) -> int:
    query = db.table(table).select("id", count="exact").limit(0)
    if column is not None:
        query = query.eq(column, value)
    res = query.execute()
    if res.count is not None:
        return int(res.count)
    return len(res.data or [])

async def _exact_count_async(
    table: str,
    column: str | None = None,
    value: Any = None,
) -> int:
    query = db.table(table).select("id", count="exact").limit(0)
    if column is not None:
        query = query.eq(column, value)
    res = await execute_async(query)
    if res.count is not None:
        return int(res.count)
    return len(res.data or [])

def _is_missing_monitor_stats_rpc(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "get_monitor_stats" in text
        and (
            "could not find" in text
            or "does not exist" in text
            or "undefined function" in text
            or "pgrst202" in text
        )
    )

def _finding_evidence_rows(finding_id: UUID, limit: int = 200) -> list[dict[str, Any]]:
    result = (
        db.table("finding_evidence")
        .select(
            "id,finding_id,evidence_key,evidence_type,source_table,source_id,"
            "observed_at,weight,excerpt_redacted,provenance,message_id,credential_id"
        )
        .eq("finding_id", str(finding_id))
        .order("observed_at", desc=True)
        .limit(max(1, min(limit, 500)))
        .execute()
    )
    return list(result.data or [])

def _target_feed_items(limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def append_item(item: dict[str, Any]) -> None:
        key = (str(item.get("target_type") or ""), str(item.get("target_value") or "").casefold())
        if key[0] not in {"domain", "url"} or not key[1] or key in seen:
            return
        seen.add(key)
        items.append(item)

    for row in _fetch_telemetry_target_rows(limit):
        if len(items) >= limit:
            break
        item = _telemetry_row_to_target(row)
        if item:
            append_item(item)

    if len(items) < limit:
        for row in _fetch_webhook_target_rows(limit):
            if len(items) >= limit:
                break
            item = _webhook_row_to_target(row)
            if item:
                append_item(item)

    return items[:limit]

def _fetch_telemetry_target_rows(limit: int) -> list[dict[str, Any]]:
    res = (
        db.table("telemetry_indicators")
        .select("indicator_type, indicator_value, first_seen_at")
        .in_("indicator_type", ["network_domain", "canonical_url"])
        .order("first_seen_at", desc=True)
        .limit(max(limit * 2, 25))
        .execute()
    )
    return list(res.data or [])

def _fetch_webhook_target_rows(limit: int) -> list[dict[str, Any]]:
    res = (
        db.table("discovered_credentials")
        .select("source, status, meta, created_at, updated_at")
        .order("created_at", desc=True)
        .limit(max(limit * 5, 100))
        .execute()
    )
    return list(res.data or [])

def _telemetry_row_to_target(row: dict[str, Any]) -> dict[str, Any] | None:
    indicator_type = str(row.get("indicator_type") or "").strip()
    raw_value = str(row.get("indicator_value") or "").strip()
    first_seen_at = str(row.get("first_seen_at") or "").strip()
    if indicator_type == "network_domain":
        target_value = _canonical_domain(raw_value)
        target_type = "domain"
        confidence = 0.85
    elif indicator_type == "canonical_url":
        target_value = _canonical_url(raw_value)
        target_type = "url"
        confidence = 0.9
    else:
        return None
    if not target_value:
        return None
    return {
        "target_type": target_type,
        "target_value": target_value,
        "source_kind": "telemetry_indicator",
        "confidence": confidence,
        "first_seen_at": first_seen_at,
        "provenance": f"telemetry_indicators.{indicator_type}",
    }

def _webhook_row_to_target(row: dict[str, Any]) -> dict[str, Any] | None:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    webhook_url = _canonical_url(str(meta.get("webhook_url") or ""))
    if not webhook_url:
        return None
    return {
        "target_type": "url",
        "target_value": webhook_url,
        "source_kind": "credential_metadata",
        "confidence": 0.8,
        "first_seen_at": str(row.get("updated_at") or row.get("created_at") or "").strip(),
        "provenance": "discovered_credentials.meta.webhook_url",
    }

def _canonical_domain(value: str) -> str:
    candidate = str(value or "").strip().lower().strip(".")
    if not candidate or "://" in candidate or "/" in candidate or "@" in candidate:
        return ""
    return candidate

def _canonical_url(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    host = str(parsed.hostname or "").lower().strip(".")
    if not host:
        return ""
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))

def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()

__all__ = ['APIRouter', 'Any', 'AuditEvent', 'AuditLogger', 'CredentialOut', 'Depends', 'EngagementLifecycleIn', 'FindingDetailOut', 'FindingEvidenceOut', 'FindingFeedbackIn', 'FindingFeedbackOut', 'FindingOut', 'FindingStatus', 'FindingType', 'HTTPException', 'JSONResponse', 'Literal', 'MessageOut', 'Query', 'StatsOut', 'StreamingResponse', 'UTC', 'UUID', '_STATS_CACHE_TTL_SECONDS', '_STATS_REDIS_KEY', '_canonical_domain', '_canonical_url', '_exact_count', '_exact_count_async', '_fetch_telemetry_target_rows', '_fetch_webhook_target_rows', '_finding_evidence_rows', '_get_monitor_stats', '_get_monitor_stats_async', '_get_monitor_stats_from_rpc', '_get_monitor_stats_from_rpc_async', '_is_missing_monitor_stats_rpc', '_monitor_stats_from_result', '_target_feed_items', '_telemetry_row_to_target', '_utc_now_iso', '_webhook_row_to_target', 'asyncio', 'csv', 'datetime', 'db', 'execute_async', 'io', 'logger', 'logging', 'require_monitor_key', 'urlsplit', 'urlunsplit']
