from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry
from celery.signals import task_retry

from app.services import bot_listener
from app.workers.tasks import audit_tasks, flow_tasks


class _FakePipeline:
    def __init__(self, redis_client):
        self.redis_client = redis_client
        self.commands = []

    def sismember(self, key, member):
        self.commands.append(("sismember", key, member))
        return self

    def sadd(self, key, member):
        self.commands.append(("sadd", key, member))
        return self

    def execute(self):
        results = []
        for operation, _key, member in self.commands:
            if operation == "sismember":
                results.append(member in self.redis_client.members)
            else:
                was_added = member not in self.redis_client.members
                self.redis_client.members.add(member)
                results.append(int(was_added))
        return results


class _FakeRedisLock:
    def __init__(self, redis_client, key):
        self.redis_client = redis_client
        self.key = key

    def acquire(self):
        with self.redis_client._lock_guard:
            if self.key in self.redis_client.lock_keys:
                return False
            self.redis_client.lock_keys.add(self.key)
            return True

    def release(self):
        with self.redis_client._lock_guard:
            self.redis_client.lock_keys.discard(self.key)


class _FakeRedis:
    def __init__(self, members=()):
        self.members = set(members)
        self.lock_keys = set()
        self._lock_guard = threading.Lock()

    def pipeline(self, **_kwargs):
        return _FakePipeline(self)

    def lock(self, key, **_kwargs):
        return _FakeRedisLock(self, key)


class _FakeQuery:
    def __init__(self, database, table):
        self.database = database
        self.table_name = table
        self.filters = {}
        self.offset = 0
        self.end = None
        self.payload = None
        self.negate_next_filter = False

    @property
    def not_(self):
        self.negate_next_filter = True
        return self

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def is_(self, column, value):
        if self.negate_next_filter:
            self.filters[column] = ("not", value)
            self.negate_next_filter = False
        else:
            self.filters[column] = None if value == "null" else value
        return self

    def in_(self, column, values):
        self.filters[column] = set(values)
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, start, end):
        self.offset = start
        self.end = end
        return self

    def update(self, payload):
        self.payload = payload
        self.database.update_calls.append((self.table_name, payload))
        return self

    def execute(self):
        rows = self.database.rows[self.table_name]
        if self.payload is not None:
            if self.database.fail_next_update:
                self.database.fail_next_update = False
                raise RuntimeError("temporary database failure")
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self.payload)
                    updated.append({"id": row["id"]})
            return SimpleNamespace(data=updated)

        matching = [row for row in rows if self._matches(row)]
        if self.table_name == "exfiltrated_messages":
            matching.sort(key=lambda row: row["id"])
        if self.end is not None:
            matching = matching[self.offset : self.end + 1]
        return SimpleNamespace(data=matching)

    def _matches(self, row):
        for column, value in self.filters.items():
            row_value = row.get(column)
            if isinstance(value, set):
                if row_value not in value:
                    return False
            elif isinstance(value, tuple) and value[0] == "not":
                if (value[1] == "null" and row_value is None) or row_value == value[1]:
                    return False
            elif row_value != value:
                return False
        return True


class _FakeDB:
    def __init__(self, credentials, messages):
        self.rows = {
            "discovered_credentials": credentials,
            "exfiltrated_messages": messages,
        }
        self.update_calls = []
        self.fail_next_update = False

    def table(self, table_name):
        return _FakeQuery(self, table_name)


def _wire_backfill_fakes(monkeypatch, credentials, messages, *, marked=()):
    database = _FakeDB(credentials, messages)
    redis_client = _FakeRedis(marked)

    async def execute(query):
        return query.execute()

    monkeypatch.setattr(audit_tasks, "db", database)
    monkeypatch.setattr(audit_tasks, "async_execute", execute)
    monkeypatch.setattr(audit_tasks, "redis_client", redis_client)
    return database, redis_client


@pytest.mark.asyncio
async def test_backfill_default_is_read_only_and_skips_marked_message(monkeypatch):
    eligible = "cred-eligible"
    invalid = "cred-general-topic"
    credentials = [
        {"id": eligible, "status": "active", "meta": {"topic_id": 42}},
        {"id": invalid, "status": "active", "meta": {"topic_id": 1}},
    ]
    messages = [
        {
            "id": "message-1",
            "credential_id": eligible,
            "telegram_msg_id": 1001,
            "is_broadcasted": True,
            "broadcast_claimed_at": None,
        },
        {
            "id": "message-2",
            "credential_id": eligible,
            "telegram_msg_id": 1002,
            "is_broadcasted": True,
            "broadcast_claimed_at": None,
        },
        {
            "id": "message-3",
            "credential_id": invalid,
            "telegram_msg_id": 1003,
            "is_broadcasted": True,
            "broadcast_claimed_at": None,
        },
    ]
    database, redis_client = _wire_backfill_fakes(
        monkeypatch,
        credentials,
        messages,
        marked={f"{eligible}:1001"},
    )
    monkeypatch.setenv("BACKFILL_BATCH_SIZE", "200")
    delayed = []
    monkeypatch.setattr(flow_tasks, "broadcast_pending", SimpleNamespace(delay=lambda: delayed.append(True)))

    result = await audit_tasks._backfill_general_messages_async()

    assert "dry run" in result.lower()
    assert "1 messages are eligible" in result
    assert database.update_calls == []
    assert redis_client.members == {f"{eligible}:1001"}
    assert delayed == []


@pytest.mark.asyncio
async def test_backfill_apply_claims_pair_and_batches_state_reset(monkeypatch):
    credential_id = "cred-eligible"
    credentials = [{"id": credential_id, "status": "active", "meta": {"topic_id": 42}}]
    messages = [
        {
            "id": f"message-{number}",
            "credential_id": credential_id,
            "telegram_msg_id": number,
            "is_broadcasted": True,
            "broadcast_claimed_at": None,
            "broadcast_message_id": 9000 + number,
            "broadcast_status": "sent",
            "broadcast_error": {"old": "failure"},
            "broadcast_attempts": 8,
            "next_retry_at": "2099-01-01T00:00:00+00:00",
        }
        for number in (1001, 1002)
    ]
    database, redis_client = _wire_backfill_fakes(monkeypatch, credentials, messages)
    monkeypatch.setenv("BACKFILL_BATCH_SIZE", "2")
    delayed = []
    monkeypatch.setattr(flow_tasks, "broadcast_pending", SimpleNamespace(delay=lambda: delayed.append(True)))

    result = await audit_tasks._backfill_general_messages_async(apply_changes=True)

    assert "requeued 2 messages" in result
    assert len(database.update_calls) == 1
    table_name, payload = database.update_calls[0]
    assert table_name == "exfiltrated_messages"
    assert payload == {
        "is_broadcasted": False,
        "broadcast_claimed_at": None,
        "broadcast_message_id": None,
        "broadcast_status": "pending",
        "broadcast_error": None,
        "broadcast_attempts": 0,
        "next_retry_at": None,
    }
    assert redis_client.members == {f"{credential_id}:1001", f"{credential_id}:1002"}
    assert delayed == [True]

    # After the normal broadcaster completes, the same pair remains excluded.
    for row in messages:
        row["is_broadcasted"] = True
    repeated = await audit_tasks._backfill_general_messages_async(apply_changes=True)
    assert "no unprocessed" in repeated.lower()
    assert len(database.update_calls) == 1
    assert delayed == [True]


def test_backfill_update_failure_is_retried_by_celery(monkeypatch):
    credential_id = "cred-eligible"
    credentials = [{"id": credential_id, "status": "active", "meta": {"topic_id": 42}}]
    messages = [
        {
            "id": "message-1001",
            "credential_id": credential_id,
            "telegram_msg_id": 1001,
            "is_broadcasted": True,
            "broadcast_claimed_at": None,
        }
    ]
    database, redis_client = _wire_backfill_fakes(monkeypatch, credentials, messages)
    database.fail_next_update = True
    monkeypatch.setenv("BACKFILL_BATCH_SIZE", "2")
    monkeypatch.setattr(audit_tasks, "_run_sync", asyncio.run)
    monkeypatch.setitem(audit_tasks.app.conf, "result_backend", "cache+memory://")
    memory_backend = audit_tasks.app._get_backend()
    monkeypatch.setattr(audit_tasks.app, "_backend_cache", memory_backend)
    monkeypatch.setattr(audit_tasks.backfill_general_messages, "store_eager_result", True)
    delayed = []
    monkeypatch.setattr(flow_tasks, "broadcast_pending", SimpleNamespace(delay=lambda: delayed.append(True)))

    retry_events = []
    retry_states = []

    def record_retry(sender=None, request=None, reason=None, **_kwargs):
        task_name = getattr(sender, "name", sender)
        if task_name == "system.backfill_general_messages":
            retry_events.append(reason)
            retry_states.append(sender.AsyncResult(request.id).state)

    task_retry.connect(record_retry, weak=False)
    try:
        result = audit_tasks.backfill_general_messages.apply(
            kwargs={"apply_changes": True},
            throw=False,
        )
    finally:
        task_retry.disconnect(record_retry)

    assert result.successful()
    assert len(retry_events) == 1
    assert isinstance(retry_events[0], Retry)
    assert "temporary database failure" in str(retry_events[0])
    assert retry_states == ["RETRY"]
    assert len(database.update_calls) == 2
    assert redis_client.members == {f"{credential_id}:1001"}
    assert messages[0]["is_broadcasted"] is False
    assert delayed == [True]


def test_overlapping_backfills_cannot_requeue_a_stale_candidate(monkeypatch):
    credential_id = "cred-eligible"
    credentials = [{"id": credential_id, "status": "active", "meta": {"topic_id": 42}}]
    messages = [
        {
            "id": "message-1001",
            "credential_id": credential_id,
            "telegram_msg_id": 1001,
            "is_broadcasted": True,
            "broadcast_claimed_at": None,
        }
    ]
    database, redis_client = _wire_backfill_fakes(monkeypatch, credentials, messages)
    monkeypatch.setenv("BACKFILL_BATCH_SIZE", "2")
    monkeypatch.setattr(audit_tasks, "_run_sync", asyncio.run)
    delayed = []
    monkeypatch.setattr(flow_tasks, "broadcast_pending", SimpleNamespace(delay=lambda: delayed.append(True)))

    update_gate = threading.Condition()
    gate_state = {
        "update_attempts": 0,
        "first_update_started": False,
        "second_update_started": False,
        "release_first": False,
        "release_second": False,
        "second_done": False,
    }

    async def execute_with_update_gate(query):
        if query.payload is not None:
            with update_gate:
                gate_state["update_attempts"] += 1
                attempt = gate_state["update_attempts"]
                if attempt in (1, 2):
                    gate_name = "first" if attempt == 1 else "second"
                    gate_state[f"{gate_name}_update_started"] = True
                    update_gate.notify_all()
                    if not update_gate.wait_for(lambda: gate_state[f"release_{gate_name}"], timeout=5):
                        raise TimeoutError("timed out waiting for the test update gate")
        return query.execute()

    monkeypatch.setattr(audit_tasks, "async_execute", execute_with_update_gate)

    def mark_second_done(_future):
        with update_gate:
            gate_state["second_done"] = True
            update_gate.notify_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_run = pool.submit(audit_tasks.backfill_general_messages.run, apply_changes=True)
        with update_gate:
            assert update_gate.wait_for(lambda: gate_state["first_update_started"], timeout=5)

        second_run = pool.submit(audit_tasks.backfill_general_messages.run, apply_changes=True)
        second_run.add_done_callback(mark_second_done)
        with update_gate:
            assert update_gate.wait_for(
                lambda: gate_state["second_update_started"] or gate_state["second_done"],
                timeout=5,
            )
            second_update_started = gate_state["second_update_started"]

        if second_update_started:
            # Reproduce the stale-candidate window: the first run completes,
            # the broadcaster marks the row sent again, then the second run
            # reaches its already-selected ID.
            with update_gate:
                gate_state["release_first"] = True
                update_gate.notify_all()
            first_result = first_run.result(timeout=5)
            messages[0]["is_broadcasted"] = True
            messages[0]["broadcast_claimed_at"] = None
            with update_gate:
                gate_state["release_second"] = True
                update_gate.notify_all()
            second_result = second_run.result(timeout=5)
        else:
            second_result = second_run.result(timeout=5)
            with update_gate:
                gate_state["release_first"] = True
                update_gate.notify_all()
            first_result = first_run.result(timeout=5)

    assert "already running" in second_result.lower()
    assert "requeued 1 messages" in first_result
    assert len(database.update_calls) == 1
    assert delayed == [True]


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return SimpleNamespace()


@pytest.mark.asyncio
@pytest.mark.parametrize(("args", "expected_apply"), [([], False), (["--dry-run"], False), (["--apply"], True)])
async def test_backfill_command_requires_explicit_apply(monkeypatch, args, expected_apply):
    queued = []

    async def do_not_wait_for_auto_delete(*_args, **_kwargs):
        pass

    monkeypatch.setattr(bot_listener, "is_admin", lambda _update: True)
    # Exercise the real reply helper while keeping its 30-second delete timer out of the test.
    monkeypatch.setattr(bot_listener, "_auto_delete_msg", do_not_wait_for_auto_delete)
    monkeypatch.setattr(
        audit_tasks,
        "backfill_general_messages",
        SimpleNamespace(delay=lambda **kwargs: queued.append(kwargs)),
    )

    message = _FakeMessage()
    await bot_listener.backfill_command(SimpleNamespace(message=message), SimpleNamespace(args=args))

    assert queued == [{"apply_changes": expected_apply}]
    assert ("dry run" in message.replies[0]["text"].lower()) is (not expected_apply)


@pytest.mark.asyncio
async def test_backfill_command_rejects_unrecognized_flags_without_queueing(monkeypatch):
    queued = []

    async def do_not_wait_for_auto_delete(*_args, **_kwargs):
        pass

    monkeypatch.setattr(bot_listener, "is_admin", lambda _update: True)
    # Exercise the real reply helper while keeping its 30-second delete timer out of the test.
    monkeypatch.setattr(bot_listener, "_auto_delete_msg", do_not_wait_for_auto_delete)
    monkeypatch.setattr(
        audit_tasks,
        "backfill_general_messages",
        SimpleNamespace(delay=lambda **kwargs: queued.append(kwargs)),
    )

    message = _FakeMessage()
    await bot_listener.backfill_command(SimpleNamespace(message=message), SimpleNamespace(args=["oops"]))

    assert queued == []
    assert "no backfill was queued" in message.replies[0]["text"].lower()


def test_celery_entrypoint_defaults_to_dry_run(monkeypatch):
    seen = []

    async def fake_backfill(*, apply_changes):
        seen.append(apply_changes)
        return "dry run"

    monkeypatch.setattr(audit_tasks, "_backfill_general_messages_async", fake_backfill)
    monkeypatch.setattr(audit_tasks, "_run_sync", asyncio.run)

    assert audit_tasks.backfill_general_messages.run() == "dry run"
    assert seen == [False]
