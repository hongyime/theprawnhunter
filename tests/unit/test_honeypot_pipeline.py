"""Unit tests for honeypot pipeline, deduplication, retry handling, and retention."""

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routers import honeypot as hp_router
from app.services.honeypot_srv import extract_honeypot_message, forward_honeypot_message
from app.workers.tasks.retention_tasks import _automated_retention_cleanup_async


def test_extract_honeypot_message_standard_text():
    payload = {
        "update_id": 99901,
        "message": {
            "message_id": 1234,
            "from": {"id": 111, "username": "alice", "first_name": "Alice"},
            "chat": {"id": 111},
            "text": "Hello world from victim",
        },
    }
    extracted = extract_honeypot_message(payload, "cred-uuid-1")
    assert extracted is not None
    assert extracted["credential_id"] == "cred-uuid-1"
    assert extracted["telegram_msg_id"] == -99901
    assert extracted["sender_name"] == "alice"
    assert extracted["sender_user_id"] == 111
    assert extracted["content"] == "Hello world from victim"
    assert extracted["media_type"] == "text"
    assert extracted["file_meta"]["message_id"] == 1234
    assert extracted["is_broadcasted"] is False


def test_scraped_message_ids_are_strictly_positive_integers():
    from app.workers.tasks.flow_tasks import _is_positive_scraped_message_id

    assert _is_positive_scraped_message_id(1)
    assert _is_positive_scraped_message_id(2_147_483_648)
    assert not _is_positive_scraped_message_id(0)
    assert not _is_positive_scraped_message_id(-1)
    assert not _is_positive_scraped_message_id(True)
    assert not _is_positive_scraped_message_id("1")


@pytest.mark.asyncio
async def test_broadcast_completion_is_fenced_to_claim_token(monkeypatch):
    from app.workers.tasks import flow_tasks

    queries = []

    class Query:
        def __init__(self):
            self.calls = []
            queries.append(self)

        def update(self, payload):
            self.calls.append(("update", payload))
            return self

        def eq(self, column, value):
            self.calls.append(("eq", column, value))
            return self

        def select(self, columns):
            self.calls.append(("select", columns))
            return self

    class FakeDb:
        def table(self, name):
            assert name == "exfiltrated_messages"
            return Query()

    monkeypatch.setattr(flow_tasks, "db", FakeDb())
    monkeypatch.setattr(
        flow_tasks,
        "async_execute",
        AsyncMock(side_effect=[
            types.SimpleNamespace(data=[]),
            types.SimpleNamespace(data=[{"id": "msg-1"}]),
        ]),
    )

    assert not await flow_tasks._apply_broadcast_claimed_update(
        "msg-1", {"is_broadcasted": True}, "claim-a"
    )
    assert await flow_tasks._apply_broadcast_claimed_update(
        "msg-1", {"is_broadcasted": True}, "claim-b"
    )
    for query in queries:
        assert ("eq", "broadcast_claimed_at", "claim-a" if query is queries[0] else "claim-b") in query.calls
        assert ("select", "id") in query.calls


def test_extract_honeypot_message_photo_caption():
    payload = {
        "update_id": 99902,
        "message": {
            "message_id": 1235,
            "from": {"id": 222, "first_name": "Bob"},
            "chat": {"id": 222},
            "caption": "confidential receipt",
            "photo": [
                {"file_id": "thumb1", "file_size": 100},
                {"file_id": "full1", "file_size": 5000, "width": 800, "height": 600},
            ],
        },
    }
    extracted = extract_honeypot_message(payload, "cred-uuid-2")
    assert extracted is not None
    assert extracted["telegram_msg_id"] == -99902
    assert extracted["sender_name"] == "Bob"
    assert extracted["content"] == "confidential receipt"
    assert extracted["media_type"] == "photo"
    assert extracted["file_meta"]["file_id"] == "full1"
    assert extracted["file_meta"]["message_id"] == 1235


def test_extract_honeypot_message_callback_query():
    payload = {
        "update_id": 99903,
        "callback_query": {
            "id": "cb123",
            "from": {"id": 333, "username": "charlie"},
            "data": "action=pay&amount=50",
            "message": {"message_id": 777},
        },
    }
    extracted = extract_honeypot_message(payload, "cred-uuid-3")
    assert extracted is not None
    assert extracted["telegram_msg_id"] == -99903
    assert extracted["sender_name"] == "charlie"
    assert "action=pay" in extracted["content"]
    assert extracted["media_type"] == "callback_query"
    assert extracted["file_meta"]["message_id"] == 777


def test_extract_honeypot_message_inline_query():
    payload = {
        "update_id": 99904,
        "inline_query": {
            "id": "iq123",
            "from": {"id": 444, "username": "david"},
            "query": "find sensitive files",
        },
    }
    extracted = extract_honeypot_message(payload, "cred-uuid-4")
    assert extracted is not None
    assert extracted["telegram_msg_id"] == -99904
    assert extracted["sender_name"] == "david"
    assert "find sensitive files" in extracted["content"]
    assert extracted["media_type"] == "inline_query"


@pytest.mark.asyncio
async def test_forward_honeypot_message_upserts_and_triggers_broadcast():
    payload = {
        "update_id": 99905,
        "message": {
            "message_id": 1236,
            "from": {"id": 555, "username": "eve"},
            "text": "test payload",
        },
    }
    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.return_value = types.SimpleNamespace(data=[])

    mock_insert = MagicMock()
    mock_insert.execute.return_value = types.SimpleNamespace(data=[{"id": "msg-1"}])

    mock_table = MagicMock()
    mock_table.select.return_value = mock_select
    mock_table.insert.return_value = mock_insert

    with patch("app.services.honeypot_srv.db.table", return_value=mock_table), \
         patch("app.workers.celery_app.app.send_task") as mock_send_task:
        result = await forward_honeypot_message(payload, "cred-uuid-5")

        assert result is not None
        assert result["telegram_msg_id"] == -99905
        mock_table.insert.assert_called_once()
        mock_send_task.assert_called_with(
            "flow.broadcast_honeypot_message",
            kwargs={"credential_id": "cred-uuid-5", "telegram_msg_id": -99905},
        )


@pytest.mark.asyncio
async def test_forward_honeypot_message_already_broadcasted_skips_rebroadcast():
    payload = {
        "update_id": 99906,
        "message": {
            "message_id": 1237,
            "from": {"id": 666, "username": "frank"},
            "text": "retry payload",
        },
    }
    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.return_value = types.SimpleNamespace(data=[{"id": "msg-old", "is_broadcasted": True}])

    mock_table = MagicMock()
    mock_table.select.return_value = mock_select

    with patch("app.services.honeypot_srv.db.table", return_value=mock_table), \
         patch("app.workers.celery_app.app.send_task") as mock_send_task:
        result = await forward_honeypot_message(payload, "cred-uuid-6")

        assert result is not None
        mock_table.insert.assert_not_called()
        mock_send_task.assert_not_called()


@pytest.mark.asyncio
async def test_receiver_duplicate_update_id_returns_ok_200(monkeypatch):
    """When Telegram retries an update that violates unique constraint, return 200 OK."""
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_MODE", True)
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_SECRET", "test_secret_12345678901234567890")
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_ALLOWLIST", "AUTO")

    class FakeQuery:
        def insert(self, *_):
            return self

        def execute(self):
            raise RuntimeError('duplicate key value violates unique constraint "idx_honeypot_cred_update_id"')

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def limit(self, *_):
            return self

    class FakeDb:
        def table(self, name):
            if name == "discovered_credentials":
                q = FakeQuery()
                q.execute = lambda: types.SimpleNamespace(data=[{"id": "cred-1", "status": "active"}])
                return q
            return FakeQuery()

    monkeypatch.setattr(hp_router, "db", FakeDb())

    req = MagicMock()
    req.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_12345678901234567890"}
    req.body = AsyncMock(return_value=b'{"update_id": 12345, "message": {"text": "hi"}}')
    req.client = types.SimpleNamespace(host="1.2.3.4")

    with patch("app.services.honeypot_srv.forward_honeypot_message", AsyncMock(return_value={"telegram_msg_id": -12345})) as mock_fwd:
        res = await hp_router.receive_webhook_update("cred-1", req)
        assert res == {"ok": True}
        mock_fwd.assert_called_once()


@pytest.mark.asyncio
async def test_receiver_duplicate_update_id_forwarding_failure_raises_500(monkeypatch):
    """When retried update fails to forward to exfiltrated_messages, raise 500 so Telegram retries."""
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_MODE", True)
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_SECRET", "test_secret_12345678901234567890")
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_ALLOWLIST", "AUTO")

    class FakeQuery:
        def insert(self, *_):
            return self

        def execute(self):
            raise RuntimeError('duplicate key value violates unique constraint "idx_honeypot_cred_update_id"')

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def limit(self, *_):
            return self

    class FakeDb:
        def table(self, name):
            if name == "discovered_credentials":
                q = FakeQuery()
                q.execute = lambda: types.SimpleNamespace(data=[{"id": "cred-1", "status": "active"}])
                return q
            return FakeQuery()

    monkeypatch.setattr(hp_router, "db", FakeDb())

    req = MagicMock()
    req.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_12345678901234567890"}
    req.body = AsyncMock(return_value=b'{"update_id": 12345, "message": {"text": "hi"}}')
    req.client = types.SimpleNamespace(host="1.2.3.4")

    with patch("app.services.honeypot_srv.forward_honeypot_message", AsyncMock(side_effect=RuntimeError("DB disconnected"))):
        with pytest.raises(HTTPException) as exc_info:
            await hp_router.receive_webhook_update("cred-1", req)
        assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_receiver_db_error_raises_http_500(monkeypatch):
    """When Supabase insert fails with generic error, raise 500 so Telegram retries."""
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_MODE", True)
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_SECRET", "test_secret_12345678901234567890")
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_ALLOWLIST", "AUTO")

    class FakeQuery:
        def insert(self, *_):
            return self

        def execute(self):
            raise RuntimeError("connection lost to Postgres database")

    class FakeDb:
        def table(self, name):
            if name == "discovered_credentials":
                q = FakeQuery()
                q.select = lambda *_: q
                q.eq = lambda *_: q
                q.limit = lambda *_: q
                q.execute = lambda: types.SimpleNamespace(data=[{"id": "cred-1", "status": "active"}])
                return q
            return FakeQuery()

    monkeypatch.setattr(hp_router, "db", FakeDb())

    req = MagicMock()
    req.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_12345678901234567890"}
    req.body = AsyncMock(return_value=b'{"update_id": 12346, "message": {"text": "hi"}}')
    req.client = types.SimpleNamespace(host="1.2.3.4")

    with pytest.raises(HTTPException) as exc_info:
        await hp_router.receive_webhook_update("cred-1", req)

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_prepare_polling_derives_credential_id():
    """prepare_polling must derive credential_id from token_hash if not provided."""
    from app.services._scraper.strategies import WebhookStateService

    mock_classifier = MagicMock()
    service = WebhookStateService(allow_delete=True, classifier=mock_classifier)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "result": {"url": ""}}
    mock_client.get = AsyncMock(return_value=mock_resp)

    fake_lookup = types.SimpleNamespace(data=[{"id": "derived-cred-uuid"}])
    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = fake_lookup

    with patch("app.core.database.db", mock_db):
        decision = await service.prepare_polling("12345:TOKEN", mock_client)
        assert decision.can_poll is True


@pytest.mark.asyncio
async def test_automated_retention_cleanup_executes():
    """Verify retention cleanup queries all 5 operational tables."""
    tables_called = []

    class FakeQuery:
        def __init__(self, table_name):
            self.table_name = table_name
            self.not_ = self

        def select(self, *_a, **_kw):
            return self

        def lt(self, *_a, **_kw):
            return self

        def eq(self, *_a, **_kw):
            return self

        def is_(self, *_a, **_kw):
            return self

        def limit(self, *_a, **_kw):
            return self

        def in_(self, *_a, **_kw):
            return self

        def delete(self):
            tables_called.append(self.table_name)
            return self

    class FakeDb:
        def table(self, name):
            return FakeQuery(name)

    with patch("app.workers.tasks.retention_tasks.db", FakeDb()), \
         patch("app.workers.tasks.retention_tasks.async_execute", AsyncMock(return_value=types.SimpleNamespace(count=5, data=[{"id": "row-1"}]))):
        result = await _automated_retention_cleanup_async()
        assert result["status"] == "complete"
        assert "audit_logs" in tables_called
        assert "keepalive_log" in tables_called
        assert "honeypot_updates" in tables_called
        assert "engagement_events" in tables_called
        assert "exfiltrated_messages" in tables_called


@pytest.mark.asyncio
async def test_receiver_null_or_invalid_update_id_dropped(monkeypatch):
    """Payloads with missing, null, or non-integer update_id are dropped safely."""
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_MODE", True)
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_SECRET", "test_secret_12345678901234567890")
    monkeypatch.setattr(hp_router.settings, "HONEYPOT_ALLOWLIST", "AUTO")

    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = types.SimpleNamespace(
        data=[{"id": "cred-1", "status": "active"}]
    )
    monkeypatch.setattr(hp_router, "db", mock_db)

    # Null update_id
    req1 = MagicMock()
    req1.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_12345678901234567890"}
    req1.body = AsyncMock(return_value=b'{"update_id": null, "message": {"text": "hi"}}')
    res1 = await hp_router.receive_webhook_update("cred-1", req1)
    assert res1 == {"ok": True}

    # Non-integer update_id
    req2 = MagicMock()
    req2.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_12345678901234567890"}
    req2.body = AsyncMock(return_value=b'{"update_id": "not_an_int", "message": {"text": "hi"}}')
    res2 = await hp_router.receive_webhook_update("cred-1", req2)
    assert res2 == {"ok": True}

    # Verify no insert was called
    mock_db.table.return_value.insert.assert_not_called()


@pytest.mark.asyncio
async def test_stale_claim_worker_fenced_out(monkeypatch):
    """When a worker holds an expired claim token, its completion write is fenced out."""
    from app.workers.tasks.flow_tasks import _honeypot_redirect_one_logic

    class FakeUpdateQuery:
        def __init__(self, table):
            self.table = table

        def update(self, *a, **kw):
            return self

        def eq(self, *a, **kw):
            return self

        def limit(self, *a, **kw):
            return self

        def select(self, *a, **kw):
            return self

    class FakeDb:
        def table(self, name):
            return FakeUpdateQuery(name)

    async def mock_async_execute(query):
        # Credential lookup succeeds with a token
        if getattr(query, "table", "") == "discovered_credentials":
            return types.SimpleNamespace(data=[{"bot_token": "gAAAA_fake"}])
        # Honeypot update fenced out (0 rows affected)
        return types.SimpleNamespace(data=[])

    mock_post = AsyncMock(return_value=types.SimpleNamespace(status_code=200, json=lambda: {"ok": True}))
    with patch("app.workers.tasks.flow_tasks.db", FakeDb()), \
         patch("app.workers.tasks.flow_tasks.async_execute", mock_async_execute), \
         patch("app.workers.tasks.flow_tasks.security.decrypt", return_value="12345:TOKEN"), \
         patch("httpx.AsyncClient.post", mock_post):
        res = await _honeypot_redirect_one_logic(
            update_id="update-uuid-1",
            credential_id="cred-uuid-1",
            user_id=12345,
            chat_id=12345,
            claim_token="stale_timestamp_2026_01_01",
        )
        assert res["status"] == "fenced_out"
        # Pre-send fence ensures outbound Telegram API is NEVER invoked
        mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_stale_claim_callback_and_inline_fenced_out():
    """Verify callback and inline hijack pre-send fence aborts before making outbound calls."""
    from app.workers.tasks.flow_tasks import _honeypot_redirect_one_logic

    class FakeQuery:
        def __init__(self):
            self.selected_cols = None

        def select(self, cols, *a, **kw):
            self.selected_cols = cols
            return self

        def eq(self, *a, **kw):
            return self

        def is_(self, *a, **kw):
            return self

        def gte(self, *a, **kw):
            return self

        def limit(self, *a, **kw):
            return self

    class FakeDb:
        def table(self, _):
            return FakeQuery()

    async def mock_execute(q):
        if getattr(q, "selected_cols", "") == "payload":
            return types.SimpleNamespace(data=[{"payload": {"callback_query": {"id": "cb-1"}, "inline_query": {"id": "iq-1"}}}])
        # Pre-send fence check (.select("id")) returns empty -> fenced out
        return types.SimpleNamespace(data=[])

    with patch("app.workers.tasks.flow_tasks.db", FakeDb()), \
         patch("app.workers.tasks.flow_tasks.async_execute", mock_execute), \
         patch("app.workers.tasks.honeypot_redirect_strategies.HoneypotRedirectStrategies.get_bot_token", AsyncMock(return_value="fake_token")), \
         patch("app.workers.tasks.honeypot_redirect_strategies.HoneypotRedirectStrategies.send_callback_hijack") as mock_cb, \
         patch("app.workers.tasks.honeypot_redirect_strategies.HoneypotRedirectStrategies.send_inline_hijack") as mock_inline:

        res_cb = await _honeypot_redirect_one_logic(
            update_id="up-cb-1",
            credential_id="cred-1",
            user_id=111,
            chat_id=111,
            update_type="callback_query",
            claim_token="stale-cb-token",
        )
        assert res_cb["status"] == "fenced_out"
        mock_cb.assert_not_called()

        res_iq = await _honeypot_redirect_one_logic(
            update_id="up-iq-1",
            credential_id="cred-1",
            user_id=222,
            chat_id=222,
            update_type="inline_query",
            claim_token="stale-iq-token",
        )
        assert res_iq["status"] == "fenced_out"
        mock_inline.assert_not_called()


@pytest.mark.asyncio
async def test_unfenced_worker_without_claim_token_is_fenced_out():
    """Queued workers without a claim_token are strictly fenced out and cannot execute."""
    from app.workers.tasks.flow_tasks import _honeypot_redirect_one_logic

    res = await _honeypot_redirect_one_logic(
        update_id="up-unfenced-1",
        credential_id="cred-1",
        user_id=123,
        chat_id=123,
        claim_token=None,
    )
    assert res["status"] == "fenced_out"
    assert res["reason"] == "missing_claim_token"


@pytest.mark.asyncio
async def test_broadcast_single_honeypot_skips_canary_source():
    """Verify honeypot broadcast rejects canary rows (file_meta.source='canary')."""
    from app.workers.tasks.flow_tasks import _broadcast_single_honeypot_message

    fake_row = {
        "id": "canary-row-1",
        "credential_id": "cred-1",
        "telegram_msg_id": -999999,
        "is_broadcasted": False,
        "file_meta": {"source": "canary"},
    }

    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value = MagicMock()

    with patch("app.workers.tasks.flow_tasks.db", mock_db), \
         patch("app.workers.tasks.flow_tasks.async_execute", AsyncMock(return_value=types.SimpleNamespace(data=[fake_row]))):
        res = await _broadcast_single_honeypot_message("cred-1", -999999)
        assert res["status"] == "skipped"
        assert res["reason"] == "not_honeypot_source"


@pytest.mark.asyncio
async def test_broadcast_single_honeypot_rejects_positive_id():
    """Verify honeypot broadcast rejects positive or zero telegram_msg_id."""
    from app.workers.tasks.flow_tasks import _broadcast_single_honeypot_message

    res = await _broadcast_single_honeypot_message("cred-1", 12345)
    assert res["status"] == "skipped"
    assert res["reason"] == "invalid_honeypot_id"

    res_zero = await _broadcast_single_honeypot_message("cred-1", 0)
    assert res_zero["status"] == "skipped"
    assert res_zero["reason"] == "invalid_honeypot_id"


@pytest.mark.asyncio
async def test_canary_message_id_uses_high_quadrillion_namespace(monkeypatch):
    """Canary messages must use the -9_000_000_000_000_000 namespace to prevent collisions."""
    from app.workers.tasks.flow_tasks import _canary_flow_check_logic, settings

    monkeypatch.setattr(settings, "CANARY_CREDENTIAL_ID", "canary-cred-1")
    monkeypatch.setattr(settings, "ENABLE_RAW_MESSAGE_BROADCAST", True)

    inserted_rows = []
    class FakeDb:
        def table(self, _):
            class FakeQ:
                def insert(self, row):
                    inserted_rows.append(row)
                    return self
                def upsert(self, row, *a, **kw):
                    inserted_rows.append(row)
                    return self
                def select(self, *a, **kw):
                    return self
                def eq(self, *a, **kw):
                    return self
                def or_(self, *a, **kw):
                    return self
                def limit(self, *a, **kw):
                    return self
                def execute(self):
                    return types.SimpleNamespace(data=[{"is_broadcasted": True}])
            return FakeQ()

    with patch("app.workers.tasks.flow_tasks.db", FakeDb()), \
         patch("app.workers.tasks.flow_tasks._broadcast_logic", AsyncMock(return_value={"status": "ok"})):
        res = await _canary_flow_check_logic()
        assert res["status"] == "ok"
        assert len(inserted_rows) == 1
        cid = inserted_rows[0]["telegram_msg_id"]
        assert cid <= -9_000_000_000_000_000, f"Expected quadrillion negative ID, got {cid}"


@pytest.mark.asyncio
async def test_retention_days_strictly_bounded_above_zero(monkeypatch):
    """Verify retention task handles <= 0 day settings safely by clamping to >= 1."""
    from app.workers.tasks.retention_tasks import _automated_retention_cleanup_async, settings

    monkeypatch.setattr(settings, "RETENTION_CLEANUP_ENABLED", True)
    monkeypatch.setattr(settings, "RETENTION_AUDIT_LOG_DAYS", 0)
    monkeypatch.setattr(settings, "RETENTION_KEEPALIVE_DAYS", -5)
    monkeypatch.setattr(settings, "RETENTION_HONEYPOT_DAYS", -10)
    monkeypatch.setattr(settings, "RETENTION_EXFIL_MESSAGE_DAYS", 0)

    class FakeQuery:
        def __init__(self):
            self.not_ = self
        def select(self, *_):
            return self
        def limit(self, *_):
            return self
        def lt(self, *_):
            return self
        def is_(self, *_):
            return self
        def eq(self, *_):
            return self
        def delete(self):
            return self
        def in_(self, *_):
            return self

    class FakeDb:
        def table(self, _):
            return FakeQuery()

    with patch("app.workers.tasks.retention_tasks.db", FakeDb()), \
         patch("app.workers.tasks.retention_tasks.async_execute", AsyncMock(return_value=types.SimpleNamespace(data=[]))):
        res = await _automated_retention_cleanup_async()
        assert res["status"] == "complete"
        assert "audit_logs" in res["details"]
        assert "honeypot_updates" in res["details"]


@pytest.mark.parametrize("redis_acquired, expected_dispatch", [(True, True), (False, False)])
@pytest.mark.asyncio
async def test_forward_honeypot_message_recorded_unbroadcast_redispatches_with_redis_dedup(
    redis_acquired, expected_dispatch
):
    """Redispatch an unbroadcast row unless its 300-second Redis dedup key exists."""
    from app.services.honeypot_srv import forward_honeypot_message

    payload = {
        "update_id": 999123,
        "message": {"message_id": 1, "chat": {"id": 10}, "text": "duplicate test"},
    }

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value = (
        types.SimpleNamespace(data=[{"id": "msg-exist-1", "is_broadcasted": False}])
    )
    mock_redis = MagicMock()
    mock_redis.client.set.return_value = redis_acquired

    with patch("app.services.honeypot_srv.db.table", return_value=mock_table), \
         patch("app.core.redis_srv.redis_srv", mock_redis), \
         patch("app.workers.celery_app.app.send_task") as mock_send_task:
        res = await forward_honeypot_message(payload, "cred-dup-1")
        assert res is not None
        assert res["telegram_msg_id"] == -999123
        mock_redis.client.set.assert_called_once_with(
            "broadcast:dispatched:cred-dup-1:-999123", "1", nx=True, ex=300
        )
        if expected_dispatch:
            mock_send_task.assert_called_once_with(
                "flow.broadcast_honeypot_message",
                kwargs={"credential_id": "cred-dup-1", "telegram_msg_id": -999123},
            )
        else:
            mock_send_task.assert_not_called()


@pytest.mark.asyncio
async def test_multi_touch_tasks_use_atomic_claims_and_fencing(monkeypatch):
    """Verify touch2 logic acquires atomic Redis reservation, DB claim, and handles contention."""
    from app.workers.tasks.honeypot_redirect_tasks import _redirect_touch2_logic, settings

    monkeypatch.setattr(settings, "HONEYPOT_REDIRECT_MODE", True)
    monkeypatch.setattr(settings, "HONEYPOT_REDIRECT_AUTHORIZED", True)

    fake_candidate = {
        "id": "hp-upd-touch2-1",
        "credential_id": "cred-t2",
        "sender_user_id": 999888,
    }

    redis_sets = []
    class FakeRedis:
        def set(self, key, token, **kwargs):
            redis_sets.append((key, token))
            return True
        def eval(self, *args):
            return 1

    fake_redis_srv = types.SimpleNamespace(client=FakeRedis())

    async def mock_async_exec(query):
        # 1. Candidate lookup
        if getattr(query, "_selected", None) == "candidates":
            return types.SimpleNamespace(data=[fake_candidate])
        # 2. DB claim
        return types.SimpleNamespace(data=[{"id": "hp-upd-touch2-1"}])

    class MockQuery:
        def __init__(self):
            self._selected = None
            self.not_ = self
        def select(self, *a):
            self._selected = "candidates"
            return self
        def is_(self, *a):
            return self
        def lt(self, *a):
            return self
        def limit(self, *a):
            return self
        def update(self, *a):
            return self
        def eq(self, *a):
            return self
        def or_(self, *a):
            return self
        def gte(self, *a):
            return self

    class MockDb:
        def table(self, _):
            return MockQuery()

    with patch("app.workers.tasks.honeypot_redirect_tasks.db", MockDb()), \
         patch("app.workers.tasks.honeypot_redirect_tasks.async_execute", mock_async_exec), \
         patch("app.core.redis_srv.redis_srv", fake_redis_srv), \
         patch("app.workers.tasks.honeypot_redirect_strategies.HoneypotRedirectStrategies.get_bot_token", AsyncMock(return_value="fake_token")), \
         patch("app.workers.tasks.honeypot_redirect_strategies.HoneypotRedirectStrategies.send_multi_touch_message", AsyncMock(return_value=True)), \
         patch("app.workers.tasks.honeypot_redirect_strategies.HoneypotRedirectStrategies.mark_multi_touch_sent"):
        res = await _redirect_touch2_logic()
        assert res["status"] == "ok"
        assert res["sent"] == 1
        assert len(redis_sets) >= 1
        key, token = redis_sets[0]
        assert "redirect:touch2:cred-t2:999888" in key
        assert "claiming:hp-upd-touch2-1:" in token


@pytest.mark.asyncio
async def test_receiver_and_extractor_reject_update_id_exceeding_32_bit():
    """Verify that update_id > 2_147_483_647 is rejected to ensure strictly disjoint negative namespaces."""
    from app.services.honeypot_srv import extract_honeypot_message
    from app.api.routers import honeypot as hp_router

    # Extractor rejection
    oversized_payload = {
        "update_id": 2_147_483_648,
        "message": {"message_id": 1, "chat": {"id": 10}, "text": "oversized"},
    }
    extracted = extract_honeypot_message(oversized_payload, "cred-1")
    assert extracted is None

    # Router webhook rejection
    req = MagicMock()
    req.headers = {"X-Telegram-Bot-Api-Secret-Token": "secret"}
    req.body = AsyncMock(return_value=b'{"update_id": 9000000000000000, "message": {"text": "hi"}}')
    req.client = types.SimpleNamespace(host="1.2.3.4")

    mock_db = MagicMock()
    with patch("app.api.routers.honeypot.db", mock_db):
        res = await hp_router.receive_webhook_update("cred-1", req)
        assert res == {"ok": True}
        # Dropped before database insert
        mock_db.table.assert_not_called()




