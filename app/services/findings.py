"""Deterministic, idempotent producers for the v1 Insight Queue.

The producer layer never stores bot tokens, raw message content, Telegram user
IDs, or usernames. It emits stable canonical keys plus redacted provenance;
the database RPC owns atomic upsert semantics and evidence-count maintenance.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.core.database import db

FINDING_TYPES = frozenset(
    {"credential_exposure", "infrastructure_cluster", "cross_bot_pattern"}
)
_SUBJECT_NAMESPACE = "theprawnhunter:cross-bot:v1"


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _is_positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str:
    return _as_utc(value).isoformat()


def _observed_range(rows: Iterable[Mapping[str, Any]], key: str) -> tuple[str, str]:
    observed = [_as_utc(row.get(key)) for row in rows]
    if not observed:
        now = datetime.now(UTC).isoformat()
        return now, now
    return min(observed).isoformat(), max(observed).isoformat()


def canonicalize_hostname(value: str | None) -> str | None:
    """Return a lower-case IDNA hostname, with ports/paths/trailing dots removed."""
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname:
            return None
        return hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None


def pseudonymize_subject(subject: Any, secret: str | None = None) -> str:
    """Create a stable one-way label without persisting the raw Telegram user ID."""
    if secret is None:
        from app.core.config import settings

        secret = settings.PSEUDONYMIZATION_KEY or settings.ENCRYPTION_KEY
    if not secret:
        raise ValueError("PSEUDONYMIZATION_KEY or ENCRYPTION_KEY is required")
    material = f"{_SUBJECT_NAMESPACE}:{subject}".encode()
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()[:24]


def _evidence_key(evidence_type: str, source_table: str, source_id: str) -> str:
    material = f"{evidence_type}:{source_table}:{source_id}".encode()
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class EvidenceRef:
    evidence_type: str
    source_table: str
    source_id: str
    observed_at: str
    weight: float = 1.0
    excerpt_redacted: str | None = None
    provenance: Mapping[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "evidence_key": _evidence_key(
                self.evidence_type, self.source_table, self.source_id
            ),
            "evidence_type": self.evidence_type,
            "source_table": self.source_table,
            "source_id": self.source_id,
            "observed_at": _iso(self.observed_at),
            "weight": _clamp(float(self.weight), 0.0, 1.0),
            "excerpt_redacted": self.excerpt_redacted,
            "provenance": dict(self.provenance or {}),
        }


@dataclass(frozen=True)
class FindingCandidate:
    finding_type: str
    canonical_key: str
    title: str
    summary: str
    why_it_matters: str
    recommended_action: str
    confidence: float
    severity: str
    priority: int
    score_explanation: Mapping[str, Any]
    first_seen_at: str
    last_seen_at: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if self.finding_type not in FINDING_TYPES:
            raise ValueError(f"unsupported finding type: {self.finding_type}")

    def as_rpc_params(self) -> dict[str, Any]:
        return {
            "p_type": self.finding_type,
            "p_canonical_key": self.canonical_key,
            "p_title": self.title,
            "p_summary": self.summary,
            "p_why_it_matters": self.why_it_matters,
            "p_recommended_action": self.recommended_action,
            "p_confidence": _clamp(float(self.confidence), 0.0, 1.0),
            "p_severity": self.severity,
            "p_priority": max(1, min(10, int(self.priority))),
            "p_score_explanation": dict(self.score_explanation),
            "p_first_seen_at": _iso(self.first_seen_at),
            "p_last_seen_at": _iso(self.last_seen_at),
            "p_evidence": [item.as_payload() for item in self.evidence],
        }


def _priority(severity: str, confidence: float, evidence_count: int) -> int:
    severity_value = {"low": 2, "medium": 5, "high": 8, "critical": 10}[severity]
    corroboration = min(2.0, max(0, evidence_count - 1) * 0.25)
    return round(_clamp(severity_value * 0.65 + confidence * 3.5 + corroboration, 1, 10))


def credential_exposure_candidates(
    rows: Sequence[Mapping[str, Any]],
) -> list[FindingCandidate]:
    candidates: list[FindingCandidate] = []
    for row in rows:
        if row.get("status") != "active" or not row.get("id"):
            continue
        credential_id = str(row["id"])
        meta = row.get("meta") if isinstance(row.get("meta"), Mapping) else {}
        webhook_present = bool(meta.get("webhook_url"))
        member_count = meta.get("chat_member_count") or row.get("chat_member_count") or 0
        member_count = member_count if isinstance(member_count, int) else 0
        message_evidence = _is_positive_number(meta.get("total_messages_scraped"))

        confidence = _clamp(
            0.65 + (0.15 if webhook_present else 0.0) + (0.1 if message_evidence else 0.0),
            0.0,
            0.95,
        )
        severity = (
            "critical"
            if member_count >= 1000 and webhook_present
            else "high"
            if webhook_present or member_count >= 100
            else "medium"
        )
        priority = _priority(severity, confidence, 1)
        observed = _iso(row.get("updated_at") or row.get("created_at"))
        source = str(row.get("source") or "unknown").split(":", 1)[0][:80]
        yield_score = row.get("collection_yield_score")
        if yield_score is None:
            yield_score = meta.get("collection_yield_score")

        candidates.append(
            FindingCandidate(
                finding_type="credential_exposure",
                canonical_key=f"credential:{credential_id}",
                title="Active Telegram credential exposure",
                summary=(
                    f"Credential {credential_id[:8]} remains active; source={source}; "
                    f"webhook={'present' if webhook_present else 'not observed'}."
                ),
                why_it_matters=(
                    "An active third-party bot credential can permit unauthorized access "
                    "to bot operations and data."
                ),
                recommended_action=(
                    "Verify ownership and authorization, notify the authorized owner, and "
                    "rotate or revoke the exposed credential."
                ),
                confidence=confidence,
                severity=severity,
                priority=priority,
                score_explanation={
                    "version": 1,
                    "confidence": {
                        "value": confidence,
                        "contributors": [
                            {"name": "validated_active", "weight": 0.65, "applied": True},
                            {"name": "webhook_observed", "weight": 0.15, "applied": webhook_present},
                            {"name": "message_evidence", "weight": 0.10, "applied": message_evidence},
                        ],
                    },
                    "severity": {
                        "value": severity,
                        "contributors": [
                            {"name": "active_credential", "weight": 1.0, "applied": True},
                            {"name": "large_audience", "weight": 0.35, "applied": member_count >= 1000},
                        ],
                    },
                    "priority": {
                        "value": priority,
                        "contributors": [
                            {"name": "severity", "weight": 0.65},
                            {"name": "confidence", "weight": 0.35},
                        ],
                    },
                    "context": {"collection_yield_score": yield_score},
                },
                first_seen_at=_iso(row.get("created_at") or observed),
                last_seen_at=observed,
                evidence=(
                    EvidenceRef(
                        evidence_type="credential_record",
                        source_table="discovered_credentials",
                        source_id=credential_id,
                        observed_at=observed,
                        excerpt_redacted=f"status=active; source={source}",
                        provenance={"producer": "credential_exposure_v1"},
                    ),
                ),
            )
        )
    return sorted(candidates, key=lambda item: item.canonical_key)


def infrastructure_cluster_candidates(
    rows: Sequence[Mapping[str, Any]],
) -> list[FindingCandidate]:
    grouped: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        meta = row.get("meta") if isinstance(row.get("meta"), Mapping) else {}
        host = canonicalize_hostname(meta.get("webhook_url"))
        if host and row.get("id"):
            grouped[host][str(row["id"])] = row

    candidates: list[FindingCandidate] = []
    for host, by_credential in sorted(grouped.items()):
        if len(by_credential) < 2:
            continue
        cluster_rows = list(by_credential.values())
        active_count = sum(row.get("status") == "active" for row in cluster_rows)
        confidence = _clamp(0.55 + min(0.35, 0.05 * len(cluster_rows)), 0.0, 0.95)
        severity = "critical" if active_count >= 5 else "high" if active_count >= 2 else "medium"
        priority = _priority(severity, confidence, len(cluster_rows))
        first_seen, last_seen = _observed_range(cluster_rows, "updated_at")
        evidence = tuple(
            EvidenceRef(
                evidence_type="shared_webhook_host",
                source_table="discovered_credentials",
                source_id=str(row["id"]),
                observed_at=_iso(row.get("updated_at") or row.get("created_at")),
                weight=0.8,
                excerpt_redacted=f"webhook_host={host}",
                provenance={"producer": "infrastructure_cluster_v1", "canonical_host": host},
            )
            for row in sorted(cluster_rows, key=lambda item: str(item["id"]))
        )
        candidates.append(
            FindingCandidate(
                finding_type="infrastructure_cluster",
                canonical_key=f"webhook-host:{host}",
                title=f"Shared webhook infrastructure: {host}",
                summary=(
                    f"{len(cluster_rows)} credentials share this canonical webhook host; "
                    f"{active_count} are currently active."
                ),
                why_it_matters=(
                    "Repeated infrastructure can connect otherwise isolated exposures, but "
                    "shared hosting alone does not prove common ownership."
                ),
                recommended_action=(
                    "Review the linked credentials and corroborate with TLS, network, or media "
                    "evidence before attributing common ownership."
                ),
                confidence=confidence,
                severity=severity,
                priority=priority,
                score_explanation={
                    "version": 1,
                    "confidence": {
                        "value": confidence,
                        "contributors": [
                            {"name": "canonical_host_match", "weight": 0.55, "applied": True},
                            {"name": "additional_credentials", "weight": 0.05, "count": len(cluster_rows) - 1},
                        ],
                    },
                    "severity": {
                        "value": severity,
                        "contributors": [{"name": "active_credentials", "weight": 1.0, "count": active_count}],
                    },
                    "priority": {
                        "value": priority,
                        "contributors": [
                            {"name": "severity", "weight": 0.65},
                            {"name": "confidence", "weight": 0.35},
                            {"name": "corroboration", "weight": 0.25, "count": len(cluster_rows)},
                        ],
                    },
                },
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                evidence=evidence,
            )
        )
    return candidates


def cross_bot_pattern_candidates(
    rows: Sequence[Mapping[str, Any]],
) -> list[FindingCandidate]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("sender_user_id") is None or not row.get("credential_id") or not row.get("id"):
            continue
        grouped[pseudonymize_subject(row["sender_user_id"])].append(row)

    candidates: list[FindingCandidate] = []
    for pseudonym, subject_rows in sorted(grouped.items()):
        credential_ids = {str(row["credential_id"]) for row in subject_rows}
        if len(credential_ids) < 2:
            continue
        unique_messages = {str(row["id"]): row for row in subject_rows}
        confidence = _clamp(0.5 + min(0.4, 0.08 * len(credential_ids)), 0.0, 0.95)
        severity = "high" if len(credential_ids) >= 5 else "medium"
        priority = _priority(severity, confidence, len(unique_messages))
        first_seen, last_seen = _observed_range(unique_messages.values(), "created_at")
        evidence = tuple(
            EvidenceRef(
                evidence_type="cross_bot_interaction",
                source_table="exfiltrated_messages",
                source_id=message_id,
                observed_at=_iso(row.get("created_at")),
                weight=0.7,
                excerpt_redacted=f"subject={pseudonym}; credential={str(row['credential_id'])[:8]}",
                provenance={"producer": "cross_bot_pattern_v1", "subject_pseudonym": pseudonym},
            )
            for message_id, row in sorted(unique_messages.items())
        )
        candidates.append(
            FindingCandidate(
                finding_type="cross_bot_pattern",
                canonical_key=f"subject:{pseudonym}",
                title=f"Cross-bot interaction pattern {pseudonym[:8]}",
                summary=(
                    f"One pseudonymous subject interacted with {len(credential_ids)} "
                    f"distinct credentials across {len(unique_messages)} evidence rows."
                ),
                why_it_matters=(
                    "A repeated subject can reveal shared audiences or coordinated activity; "
                    "it is not proof that the subject is an operator."
                ),
                recommended_action=(
                    "Inspect the redacted timeline and corroborating infrastructure before "
                    "assigning intent or identity."
                ),
                confidence=confidence,
                severity=severity,
                priority=priority,
                score_explanation={
                    "version": 1,
                    "confidence": {
                        "value": confidence,
                        "contributors": [
                            {"name": "stable_subject_pseudonym", "weight": 0.5, "applied": True},
                            {"name": "distinct_credentials", "weight": 0.08, "count": len(credential_ids)},
                        ],
                    },
                    "severity": {
                        "value": severity,
                        "contributors": [{"name": "distinct_credentials", "weight": 1.0, "count": len(credential_ids)}],
                    },
                    "priority": {
                        "value": priority,
                        "contributors": [
                            {"name": "severity", "weight": 0.65},
                            {"name": "confidence", "weight": 0.35},
                        ],
                    },
                },
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                evidence=evidence,
            )
        )
    return candidates


async def persist_candidates(
    candidates: Sequence[FindingCandidate], client: Any = db
) -> dict[str, int]:
    persisted: dict[str, int] = dict.fromkeys(FINDING_TYPES, 0)
    for start in range(0, len(candidates), 250):
        batch = candidates[start : start + 250]
        query = client.rpc(
            "upsert_findings_batch",
            {"p_candidates": [candidate.as_rpc_params() for candidate in batch]},
        )
        await asyncio.to_thread(query.execute)
        for candidate in batch:
            persisted[candidate.finding_type] += 1
    return persisted


async def produce_recent_findings(
    *, credential_limit: int = 2_000, message_limit: int = 50_000, client: Any = db
) -> dict[str, Any]:
    """Build and atomically persist a bounded, rerunnable recent-window backfill."""
    credential_result = await asyncio.to_thread(
        client.table("discovered_credentials")
        .select(
            "id,status,source,meta,created_at,updated_at,"
            "collection_yield_score,chat_member_count"
        )
        .order("updated_at", desc=True)
        .limit(max(1, min(credential_limit, 5_000)))
        .execute
    )
    message_result = await asyncio.to_thread(
        client.table("exfiltrated_messages")
        .select("id,sender_user_id,credential_id,created_at")
        .not_.is_("sender_user_id", "null")
        .order("created_at", desc=True)
        .limit(max(1, min(message_limit, 50_000)))
        .execute
    )

    credential_rows = credential_result.data or []
    message_rows = message_result.data or []
    candidates = [
        *credential_exposure_candidates(credential_rows),
        *infrastructure_cluster_candidates(credential_rows),
        *cross_bot_pattern_candidates(message_rows),
    ]
    persisted = await persist_candidates(candidates, client=client)
    return {
        "status": "ok",
        "credentials_scanned": len(credential_rows),
        "messages_scanned": len(message_rows),
        "findings_upserted": len(candidates),
        "by_type": persisted,
    }
