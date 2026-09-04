"""Monitor API contracts for the prioritized findings workflow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

AUTH = {"X-Monitor-Key": "test-monitor-key-for-pytest"}
FINDING_ID = "20000000-0000-0000-0000-000000000001"


def _finding_row():
    return {
        "id": FINDING_ID,
        "type": "credential_exposure",
        "canonical_key": "credential:30000000-0000-0000-0000-000000000001",
        "title": "Active Telegram credential exposure",
        "summary": "A redacted summary.",
        "why_it_matters": "An active credential can permit unauthorized access.",
        "recommended_action": "Verify ownership, then rotate it.",
        "confidence": 0.9,
        "severity": "high",
        "priority": 9,
        "score_explanation": {"version": 1},
        "status": "new",
        "assignee": None,
        "first_seen_at": "2026-09-04T00:00:00Z",
        "last_seen_at": "2026-09-04T01:00:00Z",
        "evidence_count": 1,
        "material_version": 2,
        "last_material_change_at": "2026-09-04T01:00:00Z",
        "created_at": "2026-09-04T00:00:00Z",
        "updated_at": "2026-09-04T01:00:00Z",
    }


def _evidence_row():
    return {
        "id": "40000000-0000-0000-0000-000000000001",
        "finding_id": FINDING_ID,
        "evidence_key": "stable-key",
        "evidence_type": "credential_record",
        "source_table": "discovered_credentials",
        "source_id": "30000000-0000-0000-0000-000000000001",
        "observed_at": "2026-09-04T01:00:00Z",
        "weight": 1,
        "excerpt_redacted": "status=active; source=github",
        "provenance": {"producer": "credential_exposure_v1"},
        "message_id": None,
        "credential_id": None,
    }


def _query(data):
    query = MagicMock()
    for method in ("select", "gte", "eq", "in_", "order", "limit"):
        getattr(query, method).return_value = query
    query.execute.return_value = SimpleNamespace(data=data)
    return query


def test_findings_list_is_priority_first_and_raw_content_free(client):
    query = _query([_finding_row()])
    fake_db = MagicMock()
    fake_db.table.return_value = query

    with patch("app.api.routers.monitor.db", fake_db):
        response = client.get("/monitor/findings?min_priority=8", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body[0]["priority"] == 9
    assert "content" not in body[0]
    query.gte.assert_called_with("priority", 8)
    query.order.assert_any_call("priority", desc=True)


def test_finding_detail_contains_redacted_evidence(client):
    finding_query = _query([_finding_row()])
    evidence_query = _query([_evidence_row()])
    fake_db = MagicMock()
    fake_db.table.side_effect = lambda table: {
        "findings": finding_query,
        "finding_evidence": evidence_query,
    }[table]

    with patch("app.api.routers.monitor.db", fake_db):
        response = client.get(f"/monitor/findings/{FINDING_ID}", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["evidence"][0]["excerpt_redacted"] == (
        "status=active; source=github"
    )
    assert "content" not in response.json()["evidence"][0]


def test_feedback_uses_service_rpc_and_pseudonymous_actor(client):
    fake_db = MagicMock()
    fake_db.rpc.return_value.execute.return_value = SimpleNamespace(
        data="50000000-0000-0000-0000-000000000001"
    )

    with patch("app.api.routers.monitor.db", fake_db):
        response = client.post(
            f"/monitor/findings/{FINDING_ID}/feedback",
            headers=AUTH,
            json={
                "label": "useful",
                "reason_code": "actionable",
                "status": "triaged",
            },
        )

    assert response.status_code == 200
    name, params = fake_db.rpc.call_args.args
    assert name == "record_finding_feedback_service"
    UUID(params["p_actor_id"])
    assert AUTH["X-Monitor-Key"] not in str(params)
    assert params["p_finding_id"] == FINDING_ID
    assert params["p_status"] == "triaged"


def test_missing_finding_returns_404(client):
    fake_db = MagicMock()
    fake_db.table.return_value = _query([])
    with patch("app.api.routers.monitor.db", fake_db):
        response = client.get(f"/monitor/findings/{FINDING_ID}", headers=AUTH)
    assert response.status_code == 404
    assert response.json() == {"detail": "Finding not found"}


def test_owned_bot_lifecycle_hashes_subject_before_persistence(client):
    recorder = AsyncMock(
        return_value={
            "status": "recorded",
            "event_type": "qualified",
            "subject_pseudonym": "abc123",
            "event_id": "60000000-0000-0000-0000-000000000001",
        }
    )
    with patch("app.services.engagement.record_engagement_event", recorder):
        response = client.post(
            "/monitor/engagement/lifecycle",
            headers=AUTH,
            json={
                "owned_bot_id": 42,
                "subject_reference": "987654321",
                "campaign_id": "launch",
                "campaign_source": "website",
                "event_type": "qualified",
                "qualification_code": "supported_intent",
            },
        )

    assert response.status_code == 200
    kwargs = recorder.await_args.kwargs
    assert kwargs["subject_id"] == "987654321"
    assert kwargs["metadata"] == {
        "entry": "monitor_api",
        "qualification_code": "supported_intent",
    }
    assert "987654321" not in response.text
