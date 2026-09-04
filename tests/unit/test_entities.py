"""Canonicalization and provenance tests for the typed evidence graph."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.entities import (
    EDGE_TYPES,
    ENTITY_TYPES,
    canonicalize_url,
    credential_graph_edges,
    interaction_graph_edges,
    media_graph_edges,
    produce_entity_graph,
    telemetry_graph_edges,
)

NOW = datetime(2026, 9, 4, tzinfo=UTC).isoformat()


def test_url_canonicalization_drops_credentials_query_and_fragment():
    assert (
        canonicalize_url("HTTPS://user:secret@Exämple.com:443/a/?token=secret#x")
        == "https://xn--exmple-cua.com/a"
    )
    assert canonicalize_url("file:///etc/passwd") is None
    assert canonicalize_url(None) is None


def test_graph_builders_emit_only_typed_edges_with_provenance():
    credential_id = "00000000-0000-0000-0000-000000000001"
    credential_edges = credential_graph_edges(
        [
            {
                "id": credential_id,
                "bot_username": "@ExampleBot",
                "created_at": NOW,
                "updated_at": NOW,
                "meta": {"webhook_url": "https://HOST.example/path"},
            }
        ]
    )
    telemetry_edges = telemetry_graph_edges(
        [
            {
                "id": "10000000-0000-0000-0000-000000000001",
                "credential_id": credential_id,
                "message_id": "20000000-0000-0000-0000-000000000001",
                "indicator_type": "network_domain",
                "indicator_value": "EXAMPLE.COM.",
                "first_seen_at": NOW,
            }
        ]
    )
    media_edges = media_graph_edges(
        [
            {
                "id": "30000000-0000-0000-0000-000000000001",
                "credential_id": credential_id,
                "sha256": "a" * 64,
                "downloaded_at": NOW,
                "error": None,
            }
        ]
    )
    edges = [*credential_edges, *telemetry_edges, *media_edges]

    assert {edge.edge_type for edge in edges} <= EDGE_TYPES
    assert {edge.source.entity_type for edge in edges} <= ENTITY_TYPES
    assert {edge.target.entity_type for edge in edges} <= ENTITY_TYPES
    assert all(edge.provenance.get("producer") for edge in edges)
    assert all(edge.evidence_source_id for edge in edges)


def test_interaction_graph_never_serializes_raw_user_id():
    raw_user_id = 9876543210123
    rows = [
        {
            "id": "10000000-0000-0000-0000-000000000001",
            "sender_user_id": raw_user_id,
            "credential_id": "00000000-0000-0000-0000-000000000001",
            "created_at": NOW,
        }
    ]
    first = interaction_graph_edges(rows)[0]
    second = interaction_graph_edges(list(rows))[0]
    payload = json.dumps(first.as_rpc_params(), sort_keys=True)

    assert first.edge_key == second.edge_key
    assert str(raw_user_id) not in payload
    assert first.source.entity_type == "user_pseudonym"


class _FakeQuery:
    def __init__(self, data: list[dict]):
        self.data = data

    @property
    def not_(self):
        return self

    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: self

    def execute(self):
        return SimpleNamespace(data=self.data)


class _FakeClient:
    def __init__(self, rows: dict[str, list[dict]]):
        self.rows = rows
        self.rpc_calls: list[tuple[str, dict]] = []

    def table(self, name: str):
        return _FakeQuery(self.rows[name])

    def rpc(self, name: str, params: dict):
        self.rpc_calls.append((name, params))
        return _FakeQuery([])


@pytest.mark.asyncio
async def test_graph_backfill_batches_stable_edges():
    credential_id = "00000000-0000-0000-0000-000000000001"
    client = _FakeClient(
        {
            "discovered_credentials": [
                {
                    "id": credential_id,
                    "bot_username": "ExampleBot",
                    "created_at": NOW,
                    "updated_at": NOW,
                    "meta": {"webhook_url": "https://host.example/a"},
                }
            ],
            "telemetry_indicators": [],
            "media_hashes": [],
            "exfiltrated_messages": [],
        }
    )

    result = await produce_entity_graph(client=client)

    assert result["edges_upserted"] == 2
    assert len(client.rpc_calls) == 1
    assert client.rpc_calls[0][0] == "upsert_entity_edges_batch"
    assert len(client.rpc_calls[0][1]["p_edges"]) == 2
