"""Policy-routed alerts for material Insight Queue changes.

Raw exfiltrated messages remain available for authenticated drill-down, but
this module sends only redacted finding summaries. Delivery claims are stored
before outbound I/O so concurrent workers do not fan out duplicate alerts.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.database import db

logger = logging.getLogger("finding_alerts")

CADENCES = frozenset({"immediate", "daily", "weekly"})
CHANNELS = frozenset({"telegram", "webhook"})
ACTIVE_FINDING_STATUSES = frozenset({"new", "triaged", "in_progress"})
_ENTITY_PREFIXES = {
    "credential": "credential:",
    "webhook_host": "webhook-host:",
    "user_pseudonym": "subject:",
    "bot": "bot:",
    "domain": "domain:",
    "url": "url:",
    "wallet": "wallet:",
    "media_hash": "media-hash:",
}

TelegramSender = Callable[[str], Awaitable[bool]]
WebhookSender = Callable[[dict[str, Any]], Awaitable[bool]]


def _as_utc(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _parse_time(value: Any) -> time | None:
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    return time.fromisoformat(str(value)).replace(tzinfo=None)


def _single_line(value: Any, limit: int) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: max(0, limit - 1)].rstrip()}…"


@dataclass(frozen=True)
class AlertPolicy:
    id: str
    name: str
    finding_type: str | None
    min_priority: int
    monitored_entity_type: str | None
    monitored_entity_value: str | None
    cadence: str
    channel: str
    timezone: str
    quiet_start: time | None
    quiet_end: time | None
    enabled: bool

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AlertPolicy:
        return cls(
            id=str(row["id"]),
            name=str(row.get("name") or "Unnamed policy"),
            finding_type=(str(row["finding_type"]) if row.get("finding_type") else None),
            min_priority=max(1, min(10, int(row.get("min_priority") or 1))),
            monitored_entity_type=(
                str(row["monitored_entity_type"])
                if row.get("monitored_entity_type")
                else None
            ),
            monitored_entity_value=(
                str(row["monitored_entity_value"])
                if row.get("monitored_entity_value")
                else None
            ),
            cadence=str(row.get("cadence") or "immediate"),
            channel=str(row.get("channel") or "telegram"),
            timezone=str(row.get("timezone") or "UTC"),
            quiet_start=_parse_time(row.get("quiet_start")),
            quiet_end=_parse_time(row.get("quiet_end")),
            enabled=bool(row.get("enabled", True)),
        )


@dataclass(frozen=True)
class RoutingDecision:
    policy_matched: bool
    deliver_now: bool
    reason: str


def _finding_entity(finding: Mapping[str, Any]) -> tuple[str | None, str | None]:
    canonical_key = str(finding.get("canonical_key") or "")
    lowered = canonical_key.casefold()
    for entity_type, prefix in _ENTITY_PREFIXES.items():
        if lowered.startswith(prefix):
            return entity_type, canonical_key[len(prefix) :]
    return None, None


def _quiet_reason(policy: AlertPolicy, evaluated_at: datetime) -> str | None:
    try:
        local_now = _as_utc(evaluated_at).astimezone(ZoneInfo(policy.timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return "invalid_timezone"

    if (policy.quiet_start is None) != (policy.quiet_end is None):
        return "invalid_quiet_hours"
    if policy.quiet_start is None or policy.quiet_end is None:
        return None

    current = local_now.timetz().replace(tzinfo=None)
    if policy.quiet_start == policy.quiet_end:
        return "quiet_hours"
    if policy.quiet_start < policy.quiet_end:
        quiet = policy.quiet_start <= current < policy.quiet_end
    else:
        quiet = current >= policy.quiet_start or current < policy.quiet_end
    return "quiet_hours" if quiet else None


def evaluate_policy(
    policy: AlertPolicy,
    finding: Mapping[str, Any],
    *,
    evaluated_at: datetime,
) -> RoutingDecision:
    if not policy.enabled:
        return RoutingDecision(False, False, "policy_disabled")
    if policy.cadence not in CADENCES or policy.channel not in CHANNELS:
        return RoutingDecision(False, False, "invalid_policy_route")
    if str(finding.get("status") or "new") not in ACTIVE_FINDING_STATUSES:
        return RoutingDecision(False, False, "finding_closed")
    if policy.finding_type and finding.get("type") != policy.finding_type:
        return RoutingDecision(False, False, "finding_type_mismatch")
    if int(finding.get("priority") or 0) < policy.min_priority:
        return RoutingDecision(False, False, "below_priority")

    if policy.monitored_entity_type:
        entity_type, entity_value = _finding_entity(finding)
        if entity_type != policy.monitored_entity_type:
            return RoutingDecision(False, False, "entity_type_mismatch")
        if (entity_value or "").casefold() != (
            policy.monitored_entity_value or ""
        ).casefold():
            return RoutingDecision(False, False, "entity_value_mismatch")

    quiet_reason = _quiet_reason(policy, evaluated_at)
    if quiet_reason:
        return RoutingDecision(True, False, quiet_reason)
    return RoutingDecision(True, True, "policy_match")


def finding_payload(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded, raw-content-free representation stored in alert audit."""
    finding_id = str(finding["id"])
    base_url = (settings.PUBLIC_FRONTEND_URL or "").rstrip("/")
    detail_url = (
        f"{base_url}/?finding={quote(finding_id)}"
        if base_url
        else f"finding:{finding_id}"
    )
    return {
        "finding_id": finding_id,
        "type": str(finding.get("type") or ""),
        "title": _single_line(finding.get("title"), 180),
        "summary": _single_line(finding.get("summary"), 280),
        "why_it_matters": _single_line(finding.get("why_it_matters"), 280),
        "recommended_action": _single_line(finding.get("recommended_action"), 280),
        "confidence": max(0.0, min(1.0, float(finding.get("confidence") or 0.0))),
        "severity": str(finding.get("severity") or "low"),
        "priority": max(1, min(10, int(finding.get("priority") or 1))),
        "evidence_count": max(0, int(finding.get("evidence_count") or 0)),
        "material_version": max(1, int(finding.get("material_version") or 1)),
        "last_material_change_at": str(finding.get("last_material_change_at") or ""),
        "detail_url": detail_url,
    }


def render_findings_digest(
    findings: Sequence[Mapping[str, Any]],
    *,
    heading: str = "Daily Top Findings",
) -> str:
    """Render at most ten priority-sorted material deltas, grouped by type."""
    ordered = sorted(
        findings,
        key=lambda row: (
            int(row.get("priority") or 0),
            str(row.get("last_material_change_at") or ""),
            str(row.get("id") or ""),
        ),
        reverse=True,
    )[:10]
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for finding in ordered:
        grouped[str(finding.get("type") or "other")].append(finding)

    lines = [f"{heading} — {len(ordered)} material delta(s)"]
    friendly_names = {
        "credential_exposure": "Credential exposure",
        "infrastructure_cluster": "Infrastructure clusters",
        "cross_bot_pattern": "Cross-bot patterns",
    }
    rank = 1
    for finding_type in (
        "credential_exposure",
        "infrastructure_cluster",
        "cross_bot_pattern",
        "other",
    ):
        rows = grouped.get(finding_type, [])
        if not rows:
            continue
        lines.append("")
        lines.append(f"{friendly_names.get(finding_type, finding_type)} ({len(rows)})")
        for row in rows:
            payload = finding_payload(row)
            lines.extend(
                (
                    f"{rank}. P{payload['priority']} {payload['severity']} — {payload['title']}",
                    (
                        f"   {payload['evidence_count']} evidence · "
                        f"confidence {round(payload['confidence'] * 100)}% · "
                        f"v{payload['material_version']}"
                    ),
                    f"   Why: {payload['why_it_matters']}",
                    f"   Open: {payload['detail_url']}",
                )
            )
            rank += 1
    return "\n".join(lines)[:3900]


async def _execute(query: Any) -> Any:
    return await asyncio.to_thread(query.execute)


async def _claim(
    client: Any,
    policy: AlertPolicy,
    finding: Mapping[str, Any],
    evaluated_at: datetime,
) -> str | None:
    payload = finding_payload(finding)
    response = await _execute(
        client.rpc(
            "claim_finding_alert",
            {
                "p_policy_id": policy.id,
                "p_finding_id": payload["finding_id"],
                "p_material_version": payload["material_version"],
                "p_cadence": policy.cadence,
                "p_channel": policy.channel,
                "p_payload_redacted": payload,
                "p_evaluated_at": _as_utc(evaluated_at).isoformat(),
            },
        )
    )
    return str(response.data) if response.data else None


async def _defer(
    client: Any,
    policy: AlertPolicy,
    finding: Mapping[str, Any],
    evaluated_at: datetime,
    reason: str,
) -> None:
    payload = finding_payload(finding)
    await _execute(
        client.rpc(
            "defer_finding_alert",
            {
                "p_policy_id": policy.id,
                "p_finding_id": payload["finding_id"],
                "p_material_version": payload["material_version"],
                "p_cadence": policy.cadence,
                "p_channel": policy.channel,
                "p_reason": reason,
                "p_payload_redacted": payload,
                "p_evaluated_at": _as_utc(evaluated_at).isoformat(),
            },
        )
    )


async def _complete(
    client: Any,
    delivery_ids: Sequence[str],
    *,
    success: bool,
    completed_at: datetime,
    reason: str,
) -> None:
    for delivery_id in delivery_ids:
        await _execute(
            client.rpc(
                "complete_finding_alert",
                {
                    "p_delivery_id": delivery_id,
                    "p_success": success,
                    "p_reason": reason,
                    "p_completed_at": _as_utc(completed_at).isoformat(),
                },
            )
        )


async def _default_telegram_sender(message: str) -> bool:
    from app.workers.tasks.flow_tasks import get_broadcaster

    return bool(await get_broadcaster().send_log(message))


async def _default_webhook_sender(payload: dict[str, Any]) -> bool:
    from app.core.webhook import dispatch_alert

    return await dispatch_alert(payload, policy_routed=True)


async def _send_claimed(
    policy: AlertPolicy,
    findings: Sequence[Mapping[str, Any]],
    *,
    telegram_sender: TelegramSender,
    webhook_sender: WebhookSender,
) -> bool:
    heading = {
        "immediate": "High-priority material finding delta",
        "daily": "Daily Top Findings",
        "weekly": "Weekly Top Findings",
    }[policy.cadence]
    message = render_findings_digest(findings, heading=heading)
    if policy.channel == "telegram":
        return bool(await telegram_sender(message))
    return bool(
        await webhook_sender(
            {
                "event": "finding_alert",
                "policy_id": policy.id,
                "policy_name": policy.name,
                "cadence": policy.cadence,
                "findings": [finding_payload(item) for item in findings],
            }
        )
    )


async def route_finding_alerts(
    cadence: str,
    *,
    evaluated_at: datetime | None = None,
    client: Any = db,
    telegram_sender: TelegramSender | None = None,
    webhook_sender: WebhookSender | None = None,
) -> dict[str, Any]:
    """Evaluate, atomically claim, and deliver one cadence of finding alerts."""
    if cadence not in CADENCES:
        raise ValueError(f"unsupported alert cadence: {cadence}")
    now = _as_utc(evaluated_at)
    telegram_send = telegram_sender or _default_telegram_sender
    webhook_send = webhook_sender or _default_webhook_sender

    policy_response, finding_response = await asyncio.gather(
        _execute(
            client.table("finding_alert_policies")
            .select("*")
            .eq("enabled", True)
            .eq("cadence", cadence)
        ),
        _execute(
            client.table("findings")
            .select(
                "id,type,canonical_key,title,summary,why_it_matters,"
                "recommended_action,confidence,severity,priority,status,"
                "evidence_count,material_version,last_material_change_at"
            )
            .in_("status", sorted(ACTIVE_FINDING_STATUSES))
            .order("priority", desc=True)
            .order("last_material_change_at", desc=True)
            .limit(500)
        ),
    )
    policies = [AlertPolicy.from_row(row) for row in (policy_response.data or [])]
    findings = list(finding_response.data or [])
    result: dict[str, Any] = {
        "status": "ok",
        "cadence": cadence,
        "policies": len(policies),
        "findings_scanned": len(findings),
        "claimed": 0,
        "delivered": 0,
        "deferred": 0,
        "failed": 0,
    }

    for policy in policies:
        matching: list[tuple[Mapping[str, Any], RoutingDecision]] = []
        for finding in findings:
            decision = evaluate_policy(policy, finding, evaluated_at=now)
            if decision.policy_matched:
                matching.append((finding, decision))

        # Bound every outbound batch to ten. Immediate policies still run every
        # five minutes, but one grouped message avoids rate-limit churn.
        max_items = 10
        claim_rows: list[Mapping[str, Any]] = []
        delivery_ids: list[str] = []
        deferred_for_policy = 0
        claimed_for_policy = 0
        for finding, decision in matching:
            if not decision.deliver_now:
                if deferred_for_policy >= max_items:
                    break
                await _defer(client, policy, finding, now, decision.reason)
                result["deferred"] += 1
                deferred_for_policy += 1
                continue
            delivery_id = await _claim(client, policy, finding, now)
            if delivery_id:
                delivery_ids.append(delivery_id)
                claim_rows.append(finding)
                result["claimed"] += 1
                claimed_for_policy += 1

            if claimed_for_policy >= max_items:
                break

        if claim_rows:
            try:
                success = await _send_claimed(
                    policy,
                    claim_rows,
                    telegram_sender=telegram_send,
                    webhook_sender=webhook_send,
                )
            except Exception:
                logger.exception("Finding alert outbound send failed")
                success = False
            await _complete(
                client,
                delivery_ids,
                success=success,
                completed_at=now,
                reason="sent" if success else "outbound_failed",
            )
            result["delivered" if success else "failed"] += len(delivery_ids)

    return result


def render_weekly_coverage(report: Mapping[str, Any]) -> str:
    return "\n".join(
        (
            "Weekly finding-alert coverage",
            f"Materially changed findings: {report['changed_findings']}",
            f"Covered by an enabled policy: {report['covered_findings']}",
            f"Uncovered: {report['uncovered_findings']}",
            f"Delivered: {report['delivered']}",
            f"Quiet/invalid deferred: {report['deferred']}",
            f"Failed: {report['failed']}",
            f"Invalid policy routes: {report['invalid_policies']}",
        )
    )


async def weekly_alert_coverage(
    *,
    evaluated_at: datetime | None = None,
    client: Any = db,
    telegram_sender: TelegramSender | None = None,
) -> dict[str, Any]:
    """Report seven-day material-delta coverage and audited delivery outcomes."""
    now = _as_utc(evaluated_at)
    since = (now - timedelta(days=7)).isoformat()
    policy_response, finding_response, delivery_response = await asyncio.gather(
        _execute(client.table("finding_alert_policies").select("*").eq("enabled", True)),
        _execute(
            client.table("findings")
            .select("id,type,canonical_key,priority,status,last_material_change_at")
            .gte("last_material_change_at", since)
            .limit(5000)
        ),
        _execute(
            client.table("finding_alert_deliveries")
            .select("finding_id,policy_id,status,reason,last_evaluated_at")
            .gte("last_evaluated_at", since)
            .limit(10000)
        ),
    )
    policies = [AlertPolicy.from_row(row) for row in (policy_response.data or [])]
    findings = list(finding_response.data or [])
    deliveries = list(delivery_response.data or [])
    valid_policies = [
        policy
        for policy in policies
        if _quiet_reason(policy, now) not in {"invalid_timezone", "invalid_quiet_hours"}
        and policy.cadence in CADENCES
        and policy.channel in CHANNELS
    ]
    covered_ids = {
        str(finding["id"])
        for finding in findings
        if any(
            evaluate_policy(policy, finding, evaluated_at=now).policy_matched
            for policy in valid_policies
        )
    }
    invalid_policies = sum(
        _quiet_reason(policy, now) in {"invalid_timezone", "invalid_quiet_hours"}
        or policy.cadence not in CADENCES
        or policy.channel not in CHANNELS
        for policy in policies
    )
    report: dict[str, Any] = {
        "status": "ok",
        "window_start": since,
        "changed_findings": len(findings),
        "covered_findings": len(covered_ids),
        "uncovered_findings": max(0, len(findings) - len(covered_ids)),
        "delivered": sum(row.get("status") == "delivered" for row in deliveries),
        "deferred": sum(row.get("status") == "deferred" for row in deliveries),
        "failed": sum(row.get("status") == "failed" for row in deliveries),
        "invalid_policies": invalid_policies,
    }
    sender = telegram_sender or _default_telegram_sender
    report["notification_sent"] = bool(await sender(render_weekly_coverage(report)))
    return report
