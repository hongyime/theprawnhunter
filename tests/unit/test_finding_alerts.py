"""Policy routing, digest, quiet-hour, and delivery idempotency tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.finding_alerts import (
    AlertPolicy,
    evaluate_policy,
    render_findings_digest,
    route_finding_alerts,
    weekly_alert_coverage,
)


def _policy(**overrides):
    row = {
        "id": "10000000-0000-0000-0000-000000000002",
        "name": "Daily Top Findings",
        "finding_type": None,
        "min_priority": 1,
        "monitored_entity_type": None,
        "monitored_entity_value": None,
        "cadence": "daily",
        "channel": "telegram",
        "timezone": "UTC",
        "quiet_start": None,
        "quiet_end": None,
        "enabled": True,
    }
    row.update(overrides)
    return row


def _finding(index: int, **overrides):
    row = {
        "id": f"20000000-0000-0000-0000-{index:012d}",
        "type": "credential_exposure",
        "canonical_key": f"credential:30000000-0000-0000-0000-{index:012d}",
        "title": f"Finding {index}",
        "summary": "A bounded redacted summary.",
        "why_it_matters": "Material risk to an authorized owner.",
        "recommended_action": "Verify ownership, then rotate the credential.",
        "confidence": 0.9,
        "severity": "high",
        "priority": 10 - (index % 10),
        "status": "new",
        "evidence_count": index + 1,
        "material_version": 1,
        "last_material_change_at": f"2026-09-04T00:{index:02d}:00+00:00",
    }
    row.update(overrides)
    return row


def test_overnight_quiet_hours_and_invalid_iana_zone_are_safe():
    finding = _finding(1)
    overnight = AlertPolicy.from_row(
        _policy(quiet_start="22:00:00", quiet_end="07:00:00")
    )

    assert evaluate_policy(
        overnight, finding, evaluated_at=datetime(2026, 9, 4, 23, tzinfo=UTC)
    ).reason == "quiet_hours"
    assert evaluate_policy(
        overnight, finding, evaluated_at=datetime(2026, 9, 4, 6, tzinfo=UTC)
    ).reason == "quiet_hours"
    assert evaluate_policy(
        overnight, finding, evaluated_at=datetime(2026, 9, 4, 12, tzinfo=UTC)
    ).deliver_now is True

    invalid = AlertPolicy.from_row(_policy(timezone="Not/A_Real_Zone"))
    decision = evaluate_policy(
        invalid, finding, evaluated_at=datetime(2026, 9, 4, 12, tzinfo=UTC)
    )
    assert decision.policy_matched is True
    assert decision.deliver_now is False
    assert decision.reason == "invalid_timezone"

    singapore = AlertPolicy.from_row(
        _policy(timezone="Asia/Singapore", quiet_start="22:00", quiet_end="07:00")
    )
    assert evaluate_policy(
        singapore, finding, evaluated_at=datetime(2026, 9, 4, 15, tzinfo=UTC)
    ).reason == "quiet_hours"


def test_type_priority_entity_and_closed_status_filters():
    finding = _finding(1)
    credential_value = finding["canonical_key"].split(":", 1)[1]
    policy = AlertPolicy.from_row(
        _policy(
            min_priority=8,
            finding_type="credential_exposure",
            monitored_entity_type="credential",
            monitored_entity_value=credential_value,
        )
    )
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)

    assert evaluate_policy(policy, finding, evaluated_at=now).deliver_now is True
    assert evaluate_policy(
        policy, {**finding, "priority": 7}, evaluated_at=now
    ).reason == "below_priority"
    assert evaluate_policy(
        policy, {**finding, "status": "suppressed"}, evaluated_at=now
    ).reason == "finding_closed"
    assert evaluate_policy(
        policy,
        {**finding, "canonical_key": "credential:different"},
        evaluated_at=now,
    ).reason == "entity_value_mismatch"


def test_digest_is_grouped_priority_sorted_and_capped_at_ten(monkeypatch):
    monkeypatch.setattr(
        "app.services.finding_alerts.settings.PUBLIC_FRONTEND_URL",
        "https://monitor.example",
    )
    findings = [
        _finding(index, type="infrastructure_cluster" if index % 2 else "credential_exposure")
        for index in range(12)
    ]

    digest = render_findings_digest(findings)

    assert "Daily Top Findings — 10 material delta(s)" in digest
    assert "Credential exposure" in digest
    assert "Infrastructure clusters" in digest
    assert digest.count("   Open: ") == 10
    assert "https://monitor.example/?finding=" in digest
    assert len(digest) <= 3900


class _FakeQuery:
    def __init__(self, data=None, execute_fn=None):
        self.data = data
        self.execute_fn = execute_fn

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: self

    def execute(self):
        if self.execute_fn:
            return self.execute_fn()
        return SimpleNamespace(data=self.data)


class _FakeClient:
    def __init__(self, policies, findings):
        self.rows = {
            "finding_alert_policies": policies,
            "findings": findings,
            "finding_alert_deliveries": [],
        }
        self.claimed = set()
        self.completed = []
        self.deferred = []

    def table(self, name):
        return _FakeQuery(self.rows[name])

    def rpc(self, name, params):
        def execute():
            if name == "claim_finding_alert":
                key = (
                    params["p_policy_id"],
                    params["p_finding_id"],
                    params["p_material_version"],
                )
                if key in self.claimed:
                    return SimpleNamespace(data=None)
                self.claimed.add(key)
                return SimpleNamespace(data=f"delivery-{len(self.claimed)}")
            if name == "complete_finding_alert":
                self.completed.append(params)
                return SimpleNamespace(data=True)
            if name == "defer_finding_alert":
                self.deferred.append(params)
                return SimpleNamespace(data="deferred")
            raise AssertionError(f"unexpected RPC {name}")

        return _FakeQuery(execute_fn=execute)


@pytest.mark.asyncio
async def test_daily_router_claims_once_batches_ten_and_then_drains_remainder():
    client = _FakeClient([_policy()], [_finding(index) for index in range(12)])
    sent = []

    async def telegram_sender(message):
        sent.append(message)
        return True

    first = await route_finding_alerts(
        "daily",
        evaluated_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
        client=client,
        telegram_sender=telegram_sender,
    )
    second = await route_finding_alerts(
        "daily",
        evaluated_at=datetime(2026, 9, 4, 12, 5, tzinfo=UTC),
        client=client,
        telegram_sender=telegram_sender,
    )
    third = await route_finding_alerts(
        "daily",
        evaluated_at=datetime(2026, 9, 4, 12, 10, tzinfo=UTC),
        client=client,
        telegram_sender=telegram_sender,
    )

    assert first["claimed"] == first["delivered"] == 10
    assert second["claimed"] == second["delivered"] == 2
    assert third["claimed"] == third["delivered"] == 0
    assert len(sent) == 2
    assert len(client.completed) == 12


@pytest.mark.asyncio
async def test_quiet_policy_defers_without_outbound_send():
    client = _FakeClient(
        [_policy(quiet_start="22:00", quiet_end="07:00")],
        [_finding(1)],
    )

    async def fail_if_sent(_message):
        raise AssertionError("quiet-hour finding must not be sent")

    result = await route_finding_alerts(
        "daily",
        evaluated_at=datetime(2026, 9, 4, 23, tzinfo=UTC),
        client=client,
        telegram_sender=fail_if_sent,
    )

    assert result["deferred"] == 1
    assert result["claimed"] == 0
    assert client.deferred[0]["p_reason"] == "quiet_hours"


@pytest.mark.asyncio
async def test_weekly_coverage_reports_uncovered_and_audited_outcomes():
    policy = _policy(min_priority=8)
    client = _FakeClient([policy], [_finding(1), _finding(2, priority=5)])
    client.rows["finding_alert_deliveries"] = [
        {
            "finding_id": _finding(1)["id"],
            "policy_id": policy["id"],
            "status": "delivered",
            "reason": "sent",
            "last_evaluated_at": "2026-09-04T12:00:00+00:00",
        }
    ]
    sent = []

    async def telegram_sender(message):
        sent.append(message)
        return True

    report = await weekly_alert_coverage(
        evaluated_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
        client=client,
        telegram_sender=telegram_sender,
    )

    assert report["changed_findings"] == 2
    assert report["covered_findings"] == 1
    assert report["uncovered_findings"] == 1
    assert report["delivered"] == 1
    assert report["notification_sent"] is True
    assert "Uncovered: 1" in sent[0]


def test_raw_message_broadcast_task_stops_before_lock_when_default_off(monkeypatch):
    from app.workers.tasks import flow_tasks

    monkeypatch.setattr(flow_tasks.settings, "ENABLE_RAW_MESSAGE_BROADCAST", False)
    monkeypatch.setattr(
        flow_tasks,
        "redis_client",
        SimpleNamespace(
            lock=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("disabled raw broadcast must not acquire a lock")
            )
        ),
    )

    assert flow_tasks.broadcast_pending().startswith("Disabled:")
