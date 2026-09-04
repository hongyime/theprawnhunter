"""Deterministic producer tests for the persistent Insight Queue."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.findings import (
    FINDING_TYPES,
    canonicalize_hostname,
    credential_exposure_candidates,
    cross_bot_pattern_candidates,
    infrastructure_cluster_candidates,
    produce_recent_findings,
    pseudonymize_subject,
)

NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _credential(identifier: str, webhook: str | None = None) -> dict:
    return {
        "id": identifier,
        "status": "active",
        "source": "github:test",
        "created_at": (NOW - timedelta(days=2)).isoformat(),
        "updated_at": NOW.isoformat(),
        "collection_yield_score": 82,
        "meta": {
            "webhook_url": webhook,
            "chat_member_count": 1200,
            "total_messages_scraped": 4,
        },
    }


def test_v1_producers_emit_only_the_three_canonical_types():
    credentials = [
        _credential("00000000-0000-0000-0000-000000000001", "HTTPS://Example.COM/a"),
        _credential("00000000-0000-0000-0000-000000000002", "https://example.com/b"),
    ]
    messages = [
        {
            "id": "10000000-0000-0000-0000-000000000001",
            "sender_user_id": 123456789,
            "credential_id": credentials[0]["id"],
            "created_at": NOW.isoformat(),
        },
        {
            "id": "10000000-0000-0000-0000-000000000002",
            "sender_user_id": 123456789,
            "credential_id": credentials[1]["id"],
            "created_at": NOW.isoformat(),
        },
    ]

    candidates = [
        *credential_exposure_candidates(credentials),
        *infrastructure_cluster_candidates(credentials),
        *cross_bot_pattern_candidates(messages),
    ]
    assert {candidate.finding_type for candidate in candidates} == FINDING_TYPES
    assert all(candidate.evidence for candidate in candidates)
    assert all(candidate.score_explanation.get("version") == 1 for candidate in candidates)


def test_hostname_canonicalization_prevents_false_split_clusters():
    assert canonicalize_hostname("HTTPS://Exämple.COM.:443/a") == "xn--exmple-cua.com"
    assert canonicalize_hostname("example.com/path") == "example.com"
    assert canonicalize_hostname(None) is None

    rows = [
        _credential("00000000-0000-0000-0000-000000000001", "https://EXAMPLE.com/a"),
        _credential("00000000-0000-0000-0000-000000000002", "example.com:443/b"),
        # Duplicate source rows do not inflate evidence or cluster size.
        _credential("00000000-0000-0000-0000-000000000002", "https://example.com/c"),
    ]
    clusters = infrastructure_cluster_candidates(rows)
    assert len(clusters) == 1
    assert clusters[0].canonical_key == "webhook-host:example.com"
    assert len(clusters[0].evidence) == 2


def test_cross_bot_subject_is_stable_and_raw_identifier_never_leaves_producer():
    raw_user_id = 9876543210123
    rows = [
        {
            "id": "10000000-0000-0000-0000-000000000001",
            "sender_user_id": raw_user_id,
            "credential_id": "00000000-0000-0000-0000-000000000001",
            "created_at": NOW.isoformat(),
        },
        {
            "id": "10000000-0000-0000-0000-000000000002",
            "sender_user_id": raw_user_id,
            "credential_id": "00000000-0000-0000-0000-000000000002",
            "created_at": (NOW + timedelta(minutes=1)).isoformat(),
        },
    ]
    first = cross_bot_pattern_candidates(rows)[0]
    second = cross_bot_pattern_candidates(list(reversed(rows)))[0]
    serialized = json.dumps(first.as_rpc_params(), sort_keys=True)

    assert first.canonical_key == second.canonical_key
    assert first.canonical_key == f"subject:{pseudonymize_subject(raw_user_id)}"
    assert str(raw_user_id) not in serialized
    assert len(first.evidence) == 2


def test_evidence_keys_and_canonical_keys_are_rerun_stable():
    row = _credential("00000000-0000-0000-0000-000000000001")
    first = credential_exposure_candidates([row])[0].as_rpc_params()
    second = credential_exposure_candidates([dict(row)])[0].as_rpc_params()

    assert first["p_canonical_key"] == second["p_canonical_key"]
    assert first["p_evidence"][0]["evidence_key"] == second["p_evidence"][0]["evidence_key"]
    assert first["p_confidence"] != first["p_score_explanation"]["context"]["collection_yield_score"]


def test_unvalidated_credentials_do_not_create_exposure_findings():
    row = _credential("00000000-0000-0000-0000-000000000001")
    row["status"] = "pending"
    assert credential_exposure_candidates([row]) == []


class _FakeQuery:
    def __init__(self, data: list[dict]):
        self.data = data

    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: self

    @property
    def not_(self):
        return self

    def execute(self):
        return SimpleNamespace(data=self.data)


class _FakeClient:
    def __init__(self, credentials: list[dict], messages: list[dict]):
        self.rows = {
            "discovered_credentials": credentials,
            "exfiltrated_messages": messages,
        }
        self.rpc_calls: list[tuple[str, dict]] = []

    def table(self, name: str):
        return _FakeQuery(self.rows[name])

    def rpc(self, name: str, params: dict):
        self.rpc_calls.append((name, params))
        return _FakeQuery([])


@pytest.mark.asyncio
async def test_bounded_backfill_batches_all_producers_into_one_rpc():
    credentials = [
        _credential("00000000-0000-0000-0000-000000000001", "example.com/a"),
        _credential("00000000-0000-0000-0000-000000000002", "example.com/b"),
    ]
    messages = [
        {
            "id": "10000000-0000-0000-0000-000000000001",
            "sender_user_id": 42,
            "credential_id": credentials[0]["id"],
            "created_at": NOW.isoformat(),
        },
        {
            "id": "10000000-0000-0000-0000-000000000002",
            "sender_user_id": 42,
            "credential_id": credentials[1]["id"],
            "created_at": NOW.isoformat(),
        },
    ]
    client = _FakeClient(credentials, messages)

    result = await produce_recent_findings(client=client)

    assert result["findings_upserted"] == 4
    assert result["by_type"] == {
        "credential_exposure": 2,
        "infrastructure_cluster": 1,
        "cross_bot_pattern": 1,
    }
    assert len(client.rpc_calls) == 1
    assert client.rpc_calls[0][0] == "upsert_findings_batch"
    assert len(client.rpc_calls[0][1]["p_candidates"]) == 4
