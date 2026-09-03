"""
Regression tests for multi-touch redirect system bug fixes.

BUG 1: celery_app.py missing honeypot_redirect_tasks import
BUG 2: flow_tasks.py sweep SELECT omits update_type column
BUG 3: redirect_one normal path references undefined `text` variable
BUG 4: redirect_1_sent_at never written on first successful send
BUG 5: Failure paths mark rows redirected/deduplicated despite failed delivery
"""
import asyncio
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# BUG 1: celery_app.py must import honeypot_redirect_tasks
# ---------------------------------------------------------------------------

def test_celery_app_imports_honeypot_redirect_tasks():
    """honeypot_redirect_tasks must be in the Celery imports list so the
    touch-2 / touch-3 / proactive-outreach tasks are registered on startup."""
    source = Path(__file__).parents[2] / "app" / "workers" / "celery_app.py"
    text = source.read_text(encoding="utf-8")
    assert "app.workers.tasks.honeypot_redirect_tasks" in text, (
        "celery_app.py must include honeypot_redirect_tasks in the imports list"
    )


# ---------------------------------------------------------------------------
# BUG 2: sweep SELECT must include update_type column
# ---------------------------------------------------------------------------

def test_sweep_select_includes_update_type():
    """The honeypot_redirect_sweep SELECT must fetch update_type so the
    callback_query / inline_query / edited_message / channel_post branches
    in honeypot_redirect_one are reachable."""
    source = Path(__file__).parents[2] / "app" / "workers" / "tasks" / "flow_tasks.py"
    text = source.read_text(encoding="utf-8")

    # Find the sweep logic block
    sweep_start = text.find("async def _honeypot_redirect_sweep_logic")
    assert sweep_start != -1, "_honeypot_redirect_sweep_logic not found"

    # Grab the first .select() call inside the sweep function
    select_start = text.find(".select(", sweep_start)
    select_end = text.find(")", select_start)
    select_expr = text[select_start:select_end]

    assert "update_type" in select_expr, (
        "Sweep SELECT must include update_type column; "
        f"found: {select_expr!r}"
    )


# ---------------------------------------------------------------------------
# BUG 3: redirect_one normal path must define `text` before sendMessage
# ---------------------------------------------------------------------------

def test_redirect_one_defines_text_before_send():
    """The normal (non-callback, non-inline) message path in
    _honeypot_redirect_one_logic must define `text` before calling
    sendMessage — previously it referenced an undefined variable."""
    source = Path(__file__).parents[2] / "app" / "workers" / "tasks" / "flow_tasks.py"
    text_src = source.read_text(encoding="utf-8")

    fn_start = text_src.find("async def _honeypot_redirect_one_logic")
    assert fn_start != -1

    # Find the sendMessage call
    send_pos = text_src.find("sendMessage", fn_start)
    assert send_pos != -1, "sendMessage not found in _honeypot_redirect_one_logic"

    # `text =` assignment must appear BEFORE sendMessage in the function body
    text_assign_pos = text_src.rfind("text = (", fn_start, send_pos)
    assert text_assign_pos != -1, (
        "`text = (` must be defined before the sendMessage call in "
        "_honeypot_redirect_one_logic"
    )


# ---------------------------------------------------------------------------
# BUG 4: redirect_1_sent_at must be written on first successful send
# ---------------------------------------------------------------------------

def test_redirect_one_writes_redirect_1_sent_at_on_success():
    """On a successful first redirect, the code must persist redirect_1_sent_at
    so the touch-2 sweep can find eligible candidates."""
    source = Path(__file__).parents[2] / "app" / "workers" / "tasks" / "flow_tasks.py"
    text_src = source.read_text(encoding="utf-8")

    fn_start = text_src.find("async def _honeypot_redirect_one_logic")
    assert fn_start != -1

    # Find the success branch update_payload block
    success_block_start = text_src.find('"redirected_at"', fn_start)
    assert success_block_start != -1, (
        '"redirected_at" key not found in _honeypot_redirect_one_logic'
    )

    # redirect_1_sent_at must appear in the same update_payload block
    # (within 500 chars of redirected_at to ensure same dict)
    nearby = text_src[success_block_start : success_block_start + 500]
    assert "redirect_1_sent_at" in nearby, (
        "redirect_1_sent_at must be written in the same update_payload as "
        "redirected_at on successful first send"
    )


# ---------------------------------------------------------------------------
# BUG 5: failure paths must NOT mark rows redirected or set dedup key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redirect_one_failure_does_not_mark_redirected(monkeypatch):
    """When sendMessage returns a non-OK response, redirected_at must NOT be
    written and the Redis dedup key must NOT be set."""
    db_updates: list[dict] = []
    redis_sets: list[str] = []

    # --- DB stub ---
    class FakeQuery:
        def __init__(self):
            self._table = None
            self._payload = None

        def update(self, payload):
            db_updates.append({"table": self._table, "payload": payload})
            return self

        def eq(self, *_):
            return self

        def select(self, *_):
            return self

        def limit(self, *_):
            return self

        def execute(self):
            return types.SimpleNamespace(data=[{"bot_token": "gAAAAfake"}])

    class FakeDb:
        def table(self, name):
            q = FakeQuery()
            q._table = name
            return q

    class FakeRedis:
        def set(self, key, value):
            redis_sets.append(key)

        def exists(self, key):
            return 0

    # --- security stub ---
    class FakeSecurity:
        def decrypt(self, _):
            return "123456:ABCfaketoken"

    # --- httpx stub: simulate failed send (ok=False) ---
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": False, "description": "Bad Request: chat not found"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *_a, **_kw):
            return FakeResponse()

    monkeypatch.setattr("app.workers.tasks.flow_tasks.db", FakeDb())
    monkeypatch.setattr("app.workers.tasks.flow_tasks.security", FakeSecurity())

    import app.workers.tasks.flow_tasks as ft

    fake_redis_srv = types.SimpleNamespace(client=FakeRedis())

    with (
        patch("app.workers.tasks.flow_tasks.async_execute", new=AsyncMock(
            return_value=types.SimpleNamespace(data=[{"bot_token": "gAAAAfake"}])
        )),
        patch("httpx.AsyncClient", FakeClient),
        patch("app.core.redis_srv.redis_srv", fake_redis_srv),
    ):
        result = await ft._honeypot_redirect_one_logic(
            update_id="upd-001",
            credential_id="cred-001",
            user_id=12345,
            chat_id=12345,
            update_type="message",
        )

    assert result["status"] == "failed"

    # redirected_at must NOT appear in any DB update payload
    for upd in db_updates:
        payload = upd.get("payload", {})
        assert "redirected_at" not in payload, (
            f"redirected_at must not be written on failure; got payload={payload}"
        )

    # Redis dedup key must NOT be set
    assert not redis_sets, (
        f"Redis dedup key must not be set on failure; got sets={redis_sets}"
    )


@pytest.mark.asyncio
async def test_redirect_one_success_marks_redirected_and_dedup(monkeypatch):
    """When sendMessage succeeds, redirected_at AND redirect_1_sent_at must be
    written, and the Redis dedup key must be set."""
    db_updates: list[dict] = []
    redis_sets: list[str] = []

    class FakeQuery:
        def __init__(self):
            self._table = None

        def update(self, payload):
            db_updates.append({"table": self._table, "payload": payload})
            return self

        def eq(self, *_):
            return self

        def select(self, *_):
            return self

        def limit(self, *_):
            return self

        def execute(self):
            return types.SimpleNamespace(data=[{"bot_token": "gAAAAfake"}])

    class FakeDb:
        def table(self, name):
            q = FakeQuery()
            q._table = name
            return q

    class FakeRedis:
        def set(self, key, value):
            redis_sets.append(key)

        def exists(self, key):
            return 0

    class FakeSecurity:
        def decrypt(self, _):
            return "123456:ABCfaketoken"

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 42}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *_a, **_kw):
            return FakeResponse()

    monkeypatch.setattr("app.workers.tasks.flow_tasks.db", FakeDb())
    monkeypatch.setattr("app.workers.tasks.flow_tasks.security", FakeSecurity())

    import app.workers.tasks.flow_tasks as ft

    fake_redis_srv = types.SimpleNamespace(client=FakeRedis())

    with (
        patch("app.workers.tasks.flow_tasks.async_execute", new=AsyncMock(
            return_value=types.SimpleNamespace(data=[{"bot_token": "gAAAAfake"}])
        )),
        patch("httpx.AsyncClient", FakeClient),
        patch("app.core.redis_srv.redis_srv", fake_redis_srv),
    ):
        result = await ft._honeypot_redirect_one_logic(
            update_id="upd-002",
            credential_id="cred-002",
            user_id=99999,
            chat_id=99999,
            update_type="message",
        )

    assert result["status"] == "sent"

    # redirected_at and redirect_1_sent_at must be in the update payload
    success_payloads = [
        upd["payload"] for upd in db_updates
        if "redirected_at" in upd.get("payload", {})
    ]
    assert success_payloads, "redirected_at must be written on success"
    assert "redirect_1_sent_at" in success_payloads[0], (
        "redirect_1_sent_at must be written on first successful send"
    )

    # Redis dedup key must be set
    assert any("redirect:sent:" in k for k in redis_sets), (
        "Redis dedup key must be set on successful delivery"
    )


@pytest.mark.asyncio
async def test_callback_failure_does_not_mark_redirected(monkeypatch):
    """When callback hijack fails (sent_ok=False), update_redirect_record and
    mark_redirect_sent must NOT be called."""
    called_update = []
    called_mark = []

    import app.workers.tasks.flow_tasks as ft
    from app.workers.tasks.honeypot_redirect_strategies import HoneypotRedirectStrategies

    async def fake_get_bot_token(_cred_id):
        return "123456:ABCfaketoken"

    async def fake_send_callback_hijack(*_a, **_kw):
        return False  # simulate failure

    async def fake_update_redirect_record(*_a, **_kw):
        called_update.append(True)

    def fake_mark_redirect_sent(*_a, **_kw):
        called_mark.append(True)

    monkeypatch.setattr(HoneypotRedirectStrategies, "get_bot_token", staticmethod(fake_get_bot_token))
    monkeypatch.setattr(HoneypotRedirectStrategies, "send_callback_hijack", staticmethod(fake_send_callback_hijack))
    monkeypatch.setattr(HoneypotRedirectStrategies, "update_redirect_record", staticmethod(fake_update_redirect_record))
    monkeypatch.setattr(HoneypotRedirectStrategies, "mark_redirect_sent", staticmethod(fake_mark_redirect_sent))

    with patch("app.workers.tasks.flow_tasks.async_execute", new=AsyncMock(
        return_value=types.SimpleNamespace(data=[{
            "payload": {"callback_query": {"id": "cb-001", "from": {"id": 111}}}
        }])
    )):
        result = await ft._honeypot_redirect_one_logic(
            update_id="upd-003",
            credential_id="cred-003",
            user_id=111,
            chat_id=111,
            update_type="callback_query",
        )

    assert result.get("sent") is False or result.get("status") == "callback_handled"
    assert not called_update, "update_redirect_record must NOT be called on callback failure"
    assert not called_mark, "mark_redirect_sent must NOT be called on callback failure"
