import asyncio
from types import SimpleNamespace

import pytest

from app.services.broadcaster_srv import BroadcastSendError


class _FakeQuery:
    def __init__(self):
        self.payload = None
        self.filters = []
        self.used_or = False

    def select(self, *_args, **_kwargs):
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    @property
    def not_(self):
        return self

    def is_(self, key, value):
        self.filters.append((f"is:{key}", value))
        return self

    def or_(self, condition):
        self.used_or = True
        self.filters.append(("or", condition))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def range(self, *_args, **_kwargs):
        return self


class _FakeDb:
    def __init__(self):
        self.queries = []

    def table(self, _table):
        query = _FakeQuery()
        self.queries.append(query)
        return query


@pytest.mark.asyncio
async def test_mark_broadcast_failure_records_error_attempts_and_retry(monkeypatch):
    from app.workers.tasks import flow_tasks

    fake_db = _FakeDb()
    captured = {}

    async def fake_async_execute(query):
        captured["payload"] = query.payload
        captured["filters"] = query.filters
        return SimpleNamespace(data=[query.payload])

    monkeypatch.setattr(flow_tasks, "db", fake_db)
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(flow_tasks.AuditLogger, "log", lambda *args, **kwargs: None)

    await flow_tasks._mark_broadcast_failure(
        {"id": "msg-1", "credential_id": "cred-1", "broadcast_attempts": 2},
        BroadcastSendError("timeout", "send exceeded timeout", retryable=True),
        claim_token="claim-1",
    )

    assert captured["filters"] == [("id", "msg-1"), ("broadcast_claimed_at", "claim-1")]
    assert captured["payload"]["broadcast_claimed_at"] is None
    assert captured["payload"]["broadcast_attempts"] == 3
    assert captured["payload"]["broadcast_error"]["reason"] == "timeout"
    assert captured["payload"]["broadcast_error"]["retryable"] is True
    assert captured["payload"]["next_retry_at"]


@pytest.mark.asyncio
async def test_mark_broadcast_failure_falls_back_when_columns_missing(monkeypatch):
    from app.workers.tasks import flow_tasks

    fake_db = _FakeDb()
    payloads = []

    async def fake_async_execute(query):
        payloads.append(query.payload)
        if len(payloads) == 1:
            raise Exception("column exfiltrated_messages.broadcast_error does not exist")
        return SimpleNamespace(data=[query.payload])

    monkeypatch.setattr(flow_tasks, "db", fake_db)
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(flow_tasks.AuditLogger, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(flow_tasks, "_BROADCAST_RELIABILITY_COLUMNS_AVAILABLE", None)
    monkeypatch.setattr(flow_tasks, "_BROADCAST_RELIABILITY_LAST_CHECK", 0.0)

    await flow_tasks._mark_broadcast_failure(
        {"id": "msg-1", "credential_id": "cred-1", "broadcast_attempts": 2},
        BroadcastSendError("timeout", "send exceeded timeout", retryable=True),
        claim_token="claim-1",
    )

    assert payloads[0]["broadcast_error"]["reason"] == "timeout"
    assert payloads[1] == {"broadcast_claimed_at": None}
    assert flow_tasks._BROADCAST_RELIABILITY_COLUMNS_AVAILABLE is False


@pytest.mark.asyncio
async def test_fetch_pending_broadcasts_falls_back_when_next_retry_missing(monkeypatch):
    from app.workers.tasks import flow_tasks

    fake_db = _FakeDb()
    calls = []

    async def fake_async_execute(query):
        calls.append(query)
        if query.used_or:
            raise Exception("column exfiltrated_messages.next_retry_at does not exist")
        return SimpleNamespace(data=[{"id": "msg-1"}])

    monkeypatch.setattr(flow_tasks, "db", fake_db)
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(flow_tasks, "_BROADCAST_RELIABILITY_COLUMNS_AVAILABLE", None)
    monkeypatch.setattr(flow_tasks, "_BROADCAST_RELIABILITY_LAST_CHECK", 0.0)

    messages = await flow_tasks._fetch_pending_broadcast_messages(10, "2026-08-02T00:00:00Z")

    assert messages == [{"id": "msg-1"}]
    assert calls[0].used_or is True
    assert calls[1].used_or is False
    assert flow_tasks._BROADCAST_RELIABILITY_COLUMNS_AVAILABLE is False


def test_broadcast_retry_delay_has_cap_and_jitter(monkeypatch):
    from app.workers.tasks import flow_tasks

    monkeypatch.setenv("BROADCAST_RETRY_MAX_DELAY_SECONDS", "600")
    monkeypatch.setenv("BROADCAST_RETRY_JITTER_RATIO", "0.10")
    monkeypatch.setattr(flow_tasks.random, "randint", lambda _low, high: high)

    delay = flow_tasks._broadcast_retry_delay_seconds(
        "flood_wait",
        retryable=True,
        retry_after_seconds=None,
    )

    assert delay == 660


@pytest.mark.asyncio
async def test_retry_failed_broadcasts_clears_delay_and_dispatches(monkeypatch):
    from app.workers.tasks import flow_tasks

    fake_db = _FakeDb()
    payloads = []
    dispatched = []
    audit_events = []

    async def fake_async_execute(query):
        if query.payload is None:
            return SimpleNamespace(
                data=[
                    {
                        "id": "msg-1",
                        "credential_id": "cred-1",
                        "broadcast_error": {"reason": "timeout"},
                    }
                ]
            )
        payloads.append(query.payload)
        return SimpleNamespace(data=[query.payload])

    monkeypatch.setattr(flow_tasks, "db", fake_db)
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(
        flow_tasks,
        "app",
        SimpleNamespace(send_task=lambda name: dispatched.append(name)),
    )
    monkeypatch.setattr(
        flow_tasks.AuditLogger,
        "log",
        lambda *args, **kwargs: audit_events.append((args, kwargs)),
    )

    result = await flow_tasks._retry_failed_broadcasts_logic(limit=10)

    assert result["status"] == "ok"
    assert payloads == [{"broadcast_claimed_at": None, "next_retry_at": None}]
    assert dispatched == ["flow.broadcast_pending"]
    assert audit_events[0][0][0] == flow_tasks.AuditEvent.BROADCAST_RETRY_REQUESTED
    assert audit_events[0][1]["user"] == "celery_worker"
    assert audit_events[0][1]["details"]["message_ids"] == ["msg-1"]


@pytest.mark.asyncio
async def test_close_revoked_topics_dry_run_reports_candidates(monkeypatch):
    from app.services import topic_admin_srv

    candidates = [
        {"id": "cred-1", "topic_id": 44, "meta": {"topic_id": 44}},
        {"id": "cred-2", "topic_id": 45, "meta": {"topic_id": 45}},
    ]

    async def fake_candidates(_limit):
        return candidates

    monkeypatch.setattr(topic_admin_srv, "fetch_revoked_topic_candidates", fake_candidates)
    monkeypatch.setattr(
        topic_admin_srv,
        "get_broadcaster",
        lambda: (_ for _ in ()).throw(AssertionError("dry-run must not close topics")),
    )

    result = await topic_admin_srv.close_revoked_topics_logic(limit=10, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["closed"] == 0
    assert result["candidate_count"] == 2
    assert result["topics"] == [
        {"credential_id": "cred-1", "topic_id": 44, "action": "would_close"},
        {"credential_id": "cred-2", "topic_id": 45, "action": "would_close"},
    ]


@pytest.mark.asyncio
async def test_close_revoked_topics_auto_disabled_skips_live_close(monkeypatch):
    from app.services import topic_admin_srv

    async def fake_candidates(_limit):
        raise AssertionError("disabled automatic close must not query candidates")

    monkeypatch.setattr(topic_admin_srv, "fetch_revoked_topic_candidates", fake_candidates)
    monkeypatch.setattr(topic_admin_srv, "settings", SimpleNamespace(AUTO_CLOSE_REVOKED_TOPICS=False))

    result = await topic_admin_srv.close_revoked_topics_logic(limit=10, dry_run=False)

    assert result == {
        "status": "disabled",
        "dry_run": False,
        "candidate_count": 0,
        "closed": 0,
        "failed": 0,
        "topics": [],
    }


@pytest.mark.asyncio
async def test_close_revoked_topics_force_overrides_auto_disabled(monkeypatch):
    from app.services import topic_admin_srv

    candidates = [{"id": "cred-1", "topic_id": 44, "meta": {"topic_id": 44}}]
    closed_topics = []
    updated_payloads = []

    async def fake_candidates(_limit):
        return candidates

    class _FakeBroadcaster:
        async def close_topic(self, group_id, topic_id):
            closed_topics.append((group_id, topic_id))
            return True

    async def fake_async_execute(query):
        if query.payload is None:
            return SimpleNamespace(data=[{"meta": {"topic_id": 44}}])
        updated_payloads.append(query.payload)
        return SimpleNamespace(data=[query.payload])

    monkeypatch.setattr(
        topic_admin_srv,
        "settings",
        SimpleNamespace(
            AUTO_CLOSE_REVOKED_TOPICS=False,
            MONITOR_GROUP_ID=-100123,
            REVOKED_TOPIC_CLOSE_DELAY_SECONDS=0,
        ),
    )
    monkeypatch.setattr(topic_admin_srv, "db", _FakeDb())
    monkeypatch.setattr(topic_admin_srv, "fetch_revoked_topic_candidates", fake_candidates)
    monkeypatch.setattr(topic_admin_srv, "get_broadcaster", lambda: _FakeBroadcaster())
    monkeypatch.setattr(topic_admin_srv, "async_execute", fake_async_execute)
    monkeypatch.setattr(topic_admin_srv.AuditLogger, "log", lambda *args, **kwargs: None)

    result = await topic_admin_srv.close_revoked_topics_logic(
        limit=10,
        dry_run=False,
        force=True,
    )

    assert result["status"] == "ok"
    assert closed_topics == [(-100123, 44)]
    assert updated_payloads[0]["meta"]["topic_status"] == "closed"


@pytest.mark.asyncio
async def test_close_revoked_topics_timeout_counts_failure(monkeypatch):
    from app.services import topic_admin_srv

    candidates = [{"id": "cred-1", "topic_id": 44, "meta": {"topic_id": 44}}]

    async def fake_candidates(_limit):
        return candidates

    class _SlowBroadcaster:
        async def close_topic(self, _group_id, _topic_id):
            await asyncio.sleep(0.05)
            return True

    monkeypatch.setattr(
        topic_admin_srv,
        "settings",
        SimpleNamespace(
            AUTO_CLOSE_REVOKED_TOPICS=True,
            MONITOR_GROUP_ID=-100123,
            REVOKED_TOPIC_CLOSE_DELAY_SECONDS=0,
            REVOKED_TOPIC_CLOSE_TIMEOUT_SECONDS=0.01,
        ),
    )
    monkeypatch.setattr(topic_admin_srv, "fetch_revoked_topic_candidates", fake_candidates)
    monkeypatch.setattr(topic_admin_srv, "get_broadcaster", lambda: _SlowBroadcaster())
    monkeypatch.setattr(topic_admin_srv.AuditLogger, "log", lambda *args, **kwargs: None)

    result = await topic_admin_srv.close_revoked_topics_logic(limit=10, dry_run=False)

    assert result["status"] == "partial"
    assert result["closed"] == 0
    assert result["failed"] == 1
    assert result["topics"] == [
        {"credential_id": "cred-1", "topic_id": 44, "closed": False}
    ]


@pytest.mark.asyncio
async def test_revoked_topic_candidates_exclude_canary(monkeypatch):
    from app.services import topic_admin_srv

    fake_db = _FakeDb()

    async def fake_async_execute(_query):
        return SimpleNamespace(
            data=[
                {"id": "canary-cred", "meta": {"topic_id": 44}},
                {"id": "revoked-cred", "meta": {"topic_id": 45}},
            ]
        )

    monkeypatch.setattr(topic_admin_srv, "db", fake_db)
    monkeypatch.setattr(topic_admin_srv, "settings", SimpleNamespace(CANARY_CREDENTIAL_ID="canary-cred"))
    monkeypatch.setattr(topic_admin_srv, "async_execute", fake_async_execute)

    candidates = await topic_admin_srv.fetch_revoked_topic_candidates(limit=10)

    assert candidates == [
        {
            "id": "revoked-cred",
            "topic_id": 45,
            "meta": {"topic_id": 45},
            "updated_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_revoked_topic_candidates_dedupe_topic_ids(monkeypatch):
    from app.services import topic_admin_srv

    fake_db = _FakeDb()

    async def fake_async_execute(_query):
        return SimpleNamespace(
            data=[
                {"id": "revoked-1", "meta": {"topic_id": 45}},
                {"id": "revoked-2", "meta": {"topic_id": 45}},
                {"id": "revoked-3", "meta": {"topic_id": 46}},
            ]
        )

    monkeypatch.setattr(topic_admin_srv, "db", fake_db)
    monkeypatch.setattr(topic_admin_srv, "settings", SimpleNamespace(CANARY_CREDENTIAL_ID=None))
    monkeypatch.setattr(topic_admin_srv, "async_execute", fake_async_execute)

    candidates = await topic_admin_srv.fetch_revoked_topic_candidates(limit=10)

    assert [(row["id"], row["topic_id"]) for row in candidates] == [
        ("revoked-1", 45),
        ("revoked-3", 46),
    ]


@pytest.mark.asyncio
async def test_close_revoked_topics_closes_and_marks_meta(monkeypatch):
    from app.services import topic_admin_srv

    fake_db = _FakeDb()
    closed_topics = []
    updated_payloads = []
    audit_events = []

    async def fake_candidates(_limit):
        return [{"id": "cred-1", "topic_id": 44, "meta": {"topic_id": 44}}]

    class _FakeBroadcaster:
        async def close_topic(self, group_id, topic_id):
            closed_topics.append((group_id, topic_id))
            return True

    async def fake_async_execute(query):
        if query.payload is None:
            return SimpleNamespace(data=[{"meta": {"topic_id": 44, "keep": "value"}}])
        updated_payloads.append(query.payload)
        return SimpleNamespace(data=[query.payload])

    monkeypatch.setattr(topic_admin_srv, "db", fake_db)
    monkeypatch.setattr(topic_admin_srv, "settings", SimpleNamespace(MONITOR_GROUP_ID=-100123))
    monkeypatch.setattr(topic_admin_srv, "fetch_revoked_topic_candidates", fake_candidates)
    monkeypatch.setattr(topic_admin_srv, "get_broadcaster", lambda: _FakeBroadcaster())
    monkeypatch.setattr(topic_admin_srv, "async_execute", fake_async_execute)
    monkeypatch.setattr(
        topic_admin_srv.AuditLogger,
        "log",
        lambda *args, **kwargs: audit_events.append((args, kwargs)),
    )

    result = await topic_admin_srv.close_revoked_topics_logic(limit=10, dry_run=False)

    assert result["status"] == "ok"
    assert result["closed"] == 1
    assert result["failed"] == 0
    assert closed_topics == [(-100123, 44)]
    assert updated_payloads[0]["meta"]["topic_status"] == "closed"
    assert updated_payloads[0]["meta"]["topic_closed_reason"] == "credential_revoked"
    assert updated_payloads[0]["meta"]["keep"] == "value"
    assert audit_events[0][0][0] == topic_admin_srv.AuditEvent.TOPIC_CLOSED


@pytest.mark.asyncio
async def test_mark_credential_revoked_queues_topic_close_when_enabled(monkeypatch):
    from app.workers.tasks import flow_tasks

    fake_db = _FakeDb()
    updates = []
    queued = []

    async def fake_async_execute(query):
        updates.append(query.payload)
        return SimpleNamespace(data=[query.payload])

    class _FakeTask:
        def apply_async(self, **kwargs):
            queued.append(kwargs)

    monkeypatch.setattr(flow_tasks, "db", fake_db)
    monkeypatch.setattr(flow_tasks, "settings", SimpleNamespace(AUTO_CLOSE_REVOKED_TOPICS=True))
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(flow_tasks, "close_revoked_topics", _FakeTask())

    await flow_tasks._mark_credential_revoked("cred-1", "unit_test")

    assert updates == [{"status": "revoked"}]
    assert queued == [
        {
            "kwargs": {
                "limit": 1,
                "dry_run": False,
                "credential_id": "cred-1",
                "reason": "unit_test",
            },
            "countdown": 5,
            "queue": "celery",
        }
    ]


@pytest.mark.asyncio
async def test_mark_credential_revoked_skips_topic_close_when_disabled(monkeypatch):
    from app.workers.tasks import flow_tasks

    fake_db = _FakeDb()
    queued = []

    async def fake_async_execute(query):
        return SimpleNamespace(data=[query.payload])

    class _FakeTask:
        def apply_async(self, **kwargs):
            queued.append(kwargs)

    monkeypatch.setattr(flow_tasks, "db", fake_db)
    monkeypatch.setattr(flow_tasks, "settings", SimpleNamespace(AUTO_CLOSE_REVOKED_TOPICS=False))
    monkeypatch.setattr(flow_tasks, "async_execute", fake_async_execute)
    monkeypatch.setattr(flow_tasks, "close_revoked_topics", _FakeTask())

    await flow_tasks._mark_credential_revoked("cred-1", "unit_test")

    assert queued == []
