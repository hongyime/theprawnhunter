import ast
import asyncio
import time
import types
from pathlib import Path

import pytest

from app.core.db_retry import DatabaseHealth
from app.services import bot_listener
from app.workers import celery_app
from app.workers.tasks import validation_tasks


def test_database_health_probe_remains_callable():
    assert callable(DatabaseHealth.check_connection)


def test_global_database_accessor_is_lazy():
    from app.core import database

    assert isinstance(database.db, database.LazyDatabaseClient)


@pytest.mark.asyncio
async def test_api_lifespan_does_not_wait_for_telegram_notification(monkeypatch):
    from app.api import main as api_main

    class SlowBroadcaster:
        async def send_log(self, _message):
            await asyncio.sleep(2)

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace())
    monkeypatch.setattr(
        "app.services.broadcaster_srv.BroadcasterService",
        SlowBroadcaster,
    )

    started = time.perf_counter()
    async with api_main.lifespan(fake_app):
        elapsed_inside = time.perf_counter() - started
    elapsed_total = time.perf_counter() - started

    assert elapsed_inside < 0.2
    assert elapsed_total < 0.4
    assert len(fake_app.state.lifecycle_notification_threads) == 2


@pytest.mark.asyncio
async def test_log_update_uses_async_monitor_guard(monkeypatch):
    calls = {"async_guard": 0, "logged": 0}

    async def fake_resolve():
        calls["async_guard"] += 1
        return {"123"}

    def fail_sync_guard(_chat_id):
        raise AssertionError("sync guard should not be called from log_update")

    def fake_log(_message):
        calls["logged"] += 1

    monkeypatch.setattr(
        "app.services.scraper_srv._resolve_monitor_group_ids_async", fake_resolve
    )
    monkeypatch.setattr("app.services.scraper_srv._is_monitor_group", fail_sync_guard)
    monkeypatch.setattr(bot_listener.logger, "info", fake_log)

    update = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=123),
        effective_user=types.SimpleNamespace(id=999),
        message=types.SimpleNamespace(text="hello", caption=None),
    )

    await bot_listener.log_update(update, context=None)

    assert calls["async_guard"] == 1
    assert calls["logged"] == 0


@pytest.mark.asyncio
async def test_log_update_never_logs_raw_identity_or_content(monkeypatch):
    logged = []

    async def fake_resolve():
        return {"123"}

    monkeypatch.setattr("app.services.scraper_srv._resolve_monitor_group_ids_async", fake_resolve)
    monkeypatch.setattr(bot_listener, "_subject_label", lambda _user_id: "subject-hash")
    monkeypatch.setattr(
        bot_listener.logger,
        "info",
        lambda message, *args: logged.append(message % args),
    )

    update = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=456, type="group"),
        effective_user=types.SimpleNamespace(id=987654321),
        message=types.SimpleNamespace(text="private payload value", caption=None),
    )

    await bot_listener.log_update(update, context=None)

    assert logged == ["🔄 Update from subject=subject-hash kind=text"]
    assert "987654321" not in logged[0]
    assert "private payload value" not in logged[0]
    assert "456" not in logged[0]


def test_captured_redirect_logs_never_include_raw_user_ids():
    root = Path(__file__).parents[2]
    redirect_source = (
        root / "app/workers/tasks/honeypot_redirect_tasks.py"
    ).read_text(encoding="utf-8")
    flow_source = (root / "app/workers/tasks/flow_tasks.py").read_text(
        encoding="utf-8"
    )
    source = f"{redirect_source}\n{flow_source}"

    assert "sent to user:{user_id}" not in source
    assert "FAILED for user:{user_id}" not in source
    assert "hijacked user:{user_id}" not in source

    task_start = flow_source.index("async def _honeypot_redirect_one_logic")
    task_end = flow_source.find("\nasync def ", task_start + 1)
    task_body = flow_source[task_start : task_end if task_end != -1 else None]
    assert '"user_id": user_id' not in task_body


def test_log_calls_never_interpolate_raw_telegram_user_ids():
    root = Path(__file__).parents[2]
    forbidden_names = {
        "sender_user_id",
        "telegram_user_id",
        "uid",
        "user_id",
        "user_identifier",
    }
    violations = []

    def contains_raw_user_id(value):
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in {"_subject_label", "pseudonymize_engagement_subject"}
        ):
            return False
        if isinstance(value, ast.Name) and value.id in forbidden_names:
            return True
        if isinstance(value, ast.Attribute) and value.attr == "id":
            if isinstance(value.value, ast.Name) and value.value.id == "user":
                return True
            if isinstance(value.value, ast.Attribute) and value.value.attr == "effective_user":
                return True
        return any(
            contains_raw_user_id(child) for child in ast.iter_child_nodes(value)
        )

    for path in (root / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_log_method = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"debug", "info", "warning", "error", "exception", "critical"}
            )
            is_print = isinstance(node.func, ast.Name) and node.func.id == "print"
            if not (is_log_method or is_print):
                continue

            for argument in node.args:
                if contains_raw_user_id(argument):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}")
                    break

    assert violations == []


def test_backfill_scoring_does_not_update_top_level_confidence_score():
    source = Path(validation_tasks.__file__).read_text(encoding="utf-8")
    forbidden = '.update({\n                        "meta": new_meta,\n                        "confidence_score": score,'
    assert forbidden not in source


def test_retry_cold_does_not_require_top_level_retry_reason_column():
    path = Path(__file__).parents[2] / "app" / "workers" / "tasks" / "scanner_tasks.py"
    source = path.read_text(encoding="utf-8")
    assert '.select("id, retry_reason")' not in source
    assert '.select("id, meta")' in source
    assert '(row.get("meta") or {}).get("retry_reason", "")' in source


def test_task_failure_audit_uses_live_audit_schema(monkeypatch):
    inserted = []

    class FakeAuditTable:
        def insert(self, payload):
            inserted.append(payload)
            return self

        def execute(self):
            return types.SimpleNamespace(data=[{}])

    class FakeDb:
        def table(self, name):
            assert name == "audit_logs"
            return FakeAuditTable()

    class InlineLoop:
        def call_soon_threadsafe(self, callback):
            callback()

        def create_task(self, coro):
            asyncio.run(coro)
            return types.SimpleNamespace()

    class InlineThread:
        def __init__(self, target, daemon=False, **_kwargs):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("app.core.database.db", FakeDb())
    monkeypatch.setattr(celery_app, "get_worker_loop", lambda: InlineLoop())
    monkeypatch.setattr(celery_app.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr("threading.Thread", InlineThread)

    celery_app.on_task_failure(
        task_id="task-1",
        exception=RuntimeError("boom"),
        traceback=None,
        einfo=None,
        args=(),
        kwargs={},
        sender=types.SimpleNamespace(name="flow.example"),
    )

    assert inserted
    payload = inserted[0]
    assert "actor" not in payload
    assert "created_at" not in payload
    assert payload["user_agent"] == "celery_worker"
    assert payload["success"] is False
    assert payload["details"]["task_name"] == "flow.example"
