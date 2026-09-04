"""Typed, evidence-backed entity graph producers for Postgres."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.database import db
from app.services.findings import canonicalize_hostname, pseudonymize_subject

ENTITY_TYPES = frozenset(
    {"credential", "bot", "webhook_host", "domain", "url", "wallet", "media_hash", "user_pseudonym"}
)
EDGE_TYPES = frozenset(
    {"represents_bot", "uses_infrastructure", "observed_indicator", "shares_media", "interacted_with"}
)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def canonicalize_url(value: str | None) -> str | None:
    """Canonicalize an HTTP(S) URL while dropping userinfo, query, and fragment."""
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        hostname = canonicalize_hostname(candidate)
        if not hostname:
            return None
        port = parsed.port
        include_port = port is not None and not (
            (parsed.scheme.lower() == "http" and port == 80)
            or (parsed.scheme.lower() == "https" and port == 443)
        )
        netloc = f"{hostname}:{port}" if include_port else hostname
        path = parsed.path.rstrip("/") or ""
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
    except ValueError:
        return None


def canonicalize_wallet(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not candidate:
        return None
    return candidate.lower() if candidate.startswith(("0x", "0X")) else candidate


@dataclass(frozen=True)
class EntityRef:
    entity_type: str
    canonical_value: str
    display_value_redacted: str
    first_seen_at: str
    last_seen_at: str
    confidence: float
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.entity_type not in ENTITY_TYPES:
            raise ValueError(f"unsupported entity type: {self.entity_type}")

    def as_payload(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "canonical_value": self.canonical_value,
            "display_value_redacted": self.display_value_redacted,
            "first_seen_at": _iso(self.first_seen_at),
            "last_seen_at": _iso(self.last_seen_at),
            "confidence": max(0.0, min(1.0, float(self.confidence))),
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class EntityEdgeCandidate:
    source: EntityRef
    target: EntityRef
    edge_type: str
    evidence_source_table: str
    evidence_source_id: str
    first_seen_at: str
    last_seen_at: str
    confidence: float
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.edge_type not in EDGE_TYPES:
            raise ValueError(f"unsupported edge type: {self.edge_type}")

    @property
    def edge_key(self) -> str:
        material = ":".join(
            (
                self.source.entity_type,
                self.source.canonical_value,
                self.edge_type,
                self.target.entity_type,
                self.target.canonical_value,
                self.evidence_source_table,
                self.evidence_source_id,
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def as_rpc_params(self) -> dict[str, Any]:
        return {
            "p_edge_key": self.edge_key,
            "p_source": self.source.as_payload(),
            "p_target": self.target.as_payload(),
            "p_edge_type": self.edge_type,
            "p_evidence_source_table": self.evidence_source_table,
            "p_evidence_source_id": self.evidence_source_id,
            "p_first_seen_at": _iso(self.first_seen_at),
            "p_last_seen_at": _iso(self.last_seen_at),
            "p_confidence": max(0.0, min(1.0, float(self.confidence))),
            "p_provenance": dict(self.provenance),
        }


def _credential_entity(credential_id: Any, observed_at: Any) -> EntityRef:
    value = str(credential_id)
    observed = _iso(observed_at)
    return EntityRef(
        "credential",
        value,
        f"credential:{value[:8]}",
        observed,
        observed,
        1.0,
        {"canonicalizer": "uuid_v1"},
    )


def credential_graph_edges(rows: Sequence[Mapping[str, Any]]) -> list[EntityEdgeCandidate]:
    edges: dict[str, EntityEdgeCandidate] = {}
    for row in rows:
        if not row.get("id"):
            continue
        observed = row.get("updated_at") or row.get("created_at")
        credential = _credential_entity(row["id"], observed)
        meta = row.get("meta") if isinstance(row.get("meta"), Mapping) else {}
        username = str(row.get("bot_username") or meta.get("bot_username") or "").strip().lstrip("@").lower()
        if username:
            bot = EntityRef(
                "bot", username, f"@{username}", _iso(observed), _iso(observed), 0.95,
                {"canonicalizer": "telegram_username_v1"},
            )
            edge = EntityEdgeCandidate(
                credential, bot, "represents_bot", "discovered_credentials", str(row["id"]),
                _iso(row.get("created_at") or observed), _iso(observed), 0.95,
                {"producer": "credential_graph_v1"},
            )
            edges[edge.edge_key] = edge

        host = canonicalize_hostname(meta.get("webhook_url"))
        if host:
            webhook = EntityRef(
                "webhook_host", host, host, _iso(observed), _iso(observed), 0.8,
                {"canonicalizer": "idna_hostname_v1"},
            )
            edge = EntityEdgeCandidate(
                credential, webhook, "uses_infrastructure", "discovered_credentials", str(row["id"]),
                _iso(row.get("created_at") or observed), _iso(observed), 0.8,
                {"producer": "credential_graph_v1", "relationship_caveat": "shared hosting is not attribution"},
            )
            edges[edge.edge_key] = edge
    return list(edges.values())


def telemetry_graph_edges(rows: Sequence[Mapping[str, Any]]) -> list[EntityEdgeCandidate]:
    type_map = {
        "network_domain": ("domain", canonicalize_hostname),
        "canonical_url": ("url", canonicalize_url),
        "wallet_address": ("wallet", canonicalize_wallet),
    }
    edges: dict[str, EntityEdgeCandidate] = {}
    for row in rows:
        mapping = type_map.get(str(row.get("indicator_type") or ""))
        if not mapping or not row.get("id") or not row.get("credential_id"):
            continue
        entity_type, canonicalizer = mapping
        canonical = canonicalizer(row.get("indicator_value"))
        if not canonical:
            continue
        observed = _iso(row.get("first_seen_at"))
        credential = _credential_entity(row["credential_id"], observed)
        indicator = EntityRef(
            entity_type, canonical, canonical, observed, observed, 0.75,
            {"canonicalizer": f"{entity_type}_v1"},
        )
        edge = EntityEdgeCandidate(
            credential, indicator, "observed_indicator", "telemetry_indicators", str(row["id"]),
            observed, observed, 0.75,
            {"producer": "telemetry_graph_v1", "message_id": str(row.get("message_id") or "")},
        )
        edges[edge.edge_key] = edge
    return list(edges.values())


def media_graph_edges(rows: Sequence[Mapping[str, Any]]) -> list[EntityEdgeCandidate]:
    edges: dict[str, EntityEdgeCandidate] = {}
    for row in rows:
        if row.get("error") or not row.get("id") or not row.get("credential_id") or not row.get("sha256"):
            continue
        sha256 = str(row["sha256"]).strip().lower()
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            continue
        observed = _iso(row.get("downloaded_at"))
        credential = _credential_entity(row["credential_id"], observed)
        media = EntityRef(
            "media_hash", sha256, f"sha256:{sha256[:12]}…", observed, observed, 0.95,
            {"canonicalizer": "sha256_v1"},
        )
        edge = EntityEdgeCandidate(
            credential, media, "shares_media", "media_hashes", str(row["id"]),
            observed, observed, 0.95, {"producer": "media_graph_v1"},
        )
        edges[edge.edge_key] = edge
    return list(edges.values())


def interaction_graph_edges(rows: Sequence[Mapping[str, Any]]) -> list[EntityEdgeCandidate]:
    edges: dict[str, EntityEdgeCandidate] = {}
    for row in rows:
        if row.get("sender_user_id") is None or not row.get("id") or not row.get("credential_id"):
            continue
        observed = _iso(row.get("created_at"))
        pseudonym = pseudonymize_subject(row["sender_user_id"])
        subject = EntityRef(
            "user_pseudonym", pseudonym, f"subject:{pseudonym[:8]}", observed, observed, 0.7,
            {"canonicalizer": "cross_bot_subject_v1"},
        )
        credential = _credential_entity(row["credential_id"], observed)
        edge = EntityEdgeCandidate(
            subject, credential, "interacted_with", "exfiltrated_messages", str(row["id"]),
            observed, observed, 0.7,
            {"producer": "interaction_graph_v1", "subject_pseudonym": pseudonym},
        )
        edges[edge.edge_key] = edge
    return list(edges.values())


async def persist_entity_edges(edges: Sequence[EntityEdgeCandidate], client: Any = db) -> int:
    for start in range(0, len(edges), 500):
        batch = edges[start : start + 500]
        await asyncio.to_thread(
            client.rpc(
                "upsert_entity_edges_batch",
                {"p_edges": [edge.as_rpc_params() for edge in batch]},
            ).execute
        )
    return len(edges)


async def produce_entity_graph(
    *, credential_limit: int = 2_000, evidence_limit: int = 50_000, client: Any = db
) -> dict[str, Any]:
    limits = max(1, min(evidence_limit, 50_000))
    credentials = await asyncio.to_thread(
        client.table("discovered_credentials")
        .select("id,bot_username,status,meta,created_at,updated_at")
        .order("updated_at", desc=True)
        .limit(max(1, min(credential_limit, 5_000)))
        .execute
    )
    telemetry = await asyncio.to_thread(
        client.table("telemetry_indicators")
        .select("id,credential_id,message_id,indicator_type,indicator_value,first_seen_at")
        .order("first_seen_at", desc=True)
        .limit(limits)
        .execute
    )
    media = await asyncio.to_thread(
        client.table("media_hashes")
        .select("id,message_id,credential_id,sha256,downloaded_at,error")
        .order("downloaded_at", desc=True)
        .limit(limits)
        .execute
    )
    messages = await asyncio.to_thread(
        client.table("exfiltrated_messages")
        .select("id,sender_user_id,credential_id,created_at")
        .not_.is_("sender_user_id", "null")
        .order("created_at", desc=True)
        .limit(limits)
        .execute
    )

    edges = [
        *credential_graph_edges(credentials.data or []),
        *telemetry_graph_edges(telemetry.data or []),
        *media_graph_edges(media.data or []),
        *interaction_graph_edges(messages.data or []),
    ]
    unique_edges = list({edge.edge_key: edge for edge in edges}.values())
    await persist_entity_edges(unique_edges, client=client)
    return {
        "status": "ok",
        "edges_upserted": len(unique_edges),
        "credentials_scanned": len(credentials.data or []),
        "telemetry_scanned": len(telemetry.data or []),
        "media_scanned": len(media.data or []),
        "messages_scanned": len(messages.data or []),
    }
