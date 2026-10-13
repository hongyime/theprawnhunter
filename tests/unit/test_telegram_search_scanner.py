import asyncio
import logging
import sqlite3
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError

from app.core.redis_srv import redis_srv
from app.services._user_agent import session_lifecycle
from app.services.user_agent_srv import user_agent
from app.workers.tasks.scanners.providers import telegram_search as telegram_search_task


class _FakeTelegramClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def __call__(self, request):
        self.calls.append((request.q, request.limit))
        return self.responder(request.q)


class _FakeLoggedOutClient:
    def __init__(self):
        self.connected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return False

    async def disconnect(self):
        self.connected = False


class _FakeSQLiteConnection:
    def execute(self, *_args):
        return None

    def close(self):
        return None


def _prepare(monkeypatch, responder):
    client = _FakeTelegramClient(responder)
    status_logs = []
    cooldowns = []
    sleeps = []

    async def start():
        return True

    async def disconnect():
        return None

    async def send_log(message):
        status_logs.append(message)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(user_agent, "lock", asyncio.Lock())
    monkeypatch.setattr(user_agent, "start", start)
    monkeypatch.setattr(user_agent, "_disconnect", disconnect)
    monkeypatch.setattr(user_agent, "client", client)
    monkeypatch.setattr(user_agent, "current_session_name", "telegram-search-test")
    monkeypatch.setattr(telegram_search_task, "redis_client", SimpleNamespace(get=lambda _key: None))
    monkeypatch.setattr(telegram_search_task, "_send_log_async", send_log)
    monkeypatch.setattr(telegram_search_task.asyncio, "sleep", sleep)
    monkeypatch.setattr(redis_srv, "set_cooldown", lambda key, seconds: cooldowns.append((key, seconds)))

    return client, status_logs, cooldowns, sleeps


@pytest.mark.asyncio
async def test_service_down_is_logged_and_later_searches_continue(monkeypatch, caplog):
    first_query = "api.telegram.org/bot"

    def reply(query):
        if query == first_query:
            raise OSError("Telegram service cannot be reached")
        return SimpleNamespace(chats=[], messages=[])

    client, status_logs, _, sleeps = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    result = await telegram_search_task._scan_telegram_search_async()

    assert result == "TelegramSearch finished. 0 matches."
    assert [query for query, _limit in client.calls] == [
        "api.telegram.org/bot",
        "TELEGRAM_BOT_TOKEN",
        "bot_token leaked",
    ]
    assert all(limit == 100 for _query, limit in client.calls)
    assert sleeps == [3.0, 5, 3.0, 5, 3.0]
    assert first_query in caplog.text
    assert "OSError: Telegram service cannot be reached" in caplog.text
    assert "Moving to the next search" in caplog.text
    assert any(first_query in message and "next scan" in message for message in status_logs)


@pytest.mark.asyncio
async def test_floodwait_waits_fully_and_stops_more_searches(monkeypatch, caplog):
    wait_seconds = 23

    def reply(_query):
        raise FloodWaitError(None, capture=wait_seconds)

    client, status_logs, cooldowns, sleeps = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    result = await telegram_search_task._scan_telegram_search_async()

    assert result == f"TelegramSearch paused after FloodWait for {wait_seconds}s; remaining searches will run on the next scan."
    assert len(client.calls) == 1
    assert client.calls[0] == ("api.telegram.org/bot", 100)
    assert sleeps == [3.0, wait_seconds]
    assert cooldowns == [("user_agent:cooldown:telegram-search-test", wait_seconds + 60)]
    assert "FloodWait" in caplog.text and "api.telegram.org/bot" in caplog.text
    assert f"full {wait_seconds}s" in caplog.text
    assert any("FloodWait" in message and "next scan" in message for message in status_logs)


@pytest.mark.asyncio
async def test_logged_out_session_is_reported_and_scan_stops_cleanly(monkeypatch, caplog, tmp_path):
    client, status_logs, _, sleeps = _prepare(
        monkeypatch,
        lambda _query: SimpleNamespace(chats=[], messages=[]),
    )
    fake_session = tmp_path / "logged-out.session"
    fake_telegram_client = _FakeLoggedOutClient()

    # Exercise the real session start and search code with fake Telegram,
    # Redis, database lease, and file boundaries. No real session is read.
    monkeypatch.setattr(user_agent, "start", type(user_agent).start.__get__(user_agent))
    monkeypatch.setattr(user_agent, "_tmp_archive_sweep_done", True)
    monkeypatch.setattr(user_agent, "_refresher_task", object())
    monkeypatch.setattr(user_agent, "_ensure_task", SimpleNamespace(done=lambda: False))
    monkeypatch.setattr(user_agent, "sessions", [str(fake_session)])
    monkeypatch.setattr(user_agent, "client", None)
    monkeypatch.setattr(user_agent, "_session_cooldowns", {})
    monkeypatch.setattr(user_agent, "_session_failure_history", {})
    monkeypatch.setattr(user_agent, "_session_cooldown_started", {})
    monkeypatch.setattr(user_agent, "_session_lock_key", None)
    monkeypatch.setattr(user_agent, "_current_phone", None)
    monkeypatch.setattr(user_agent, "_last_failure_reason", None)
    monkeypatch.setattr(user_agent, "_all_on_cooldown_warned_at", None)
    monkeypatch.setattr(session_lifecycle, "TelegramClient", lambda *_args, **_kwargs: fake_telegram_client)
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: _FakeSQLiteConnection())

    async def acquire_lease(_path):
        return True

    async def release_lease():
        return None

    monkeypatch.setattr(user_agent, "_acquire_db_lease", acquire_lease)
    monkeypatch.setattr(user_agent, "_release_db_lease", release_lease)
    monkeypatch.setattr(user_agent, "_cleanup_temp_session", lambda _filename: None)
    monkeypatch.setattr(redis_srv, "get_next_rotation_index", lambda *_args: 0)
    monkeypatch.setattr(redis_srv, "get_cooldown_remaining", lambda *_args: 0)
    monkeypatch.setattr(redis_srv, "is_on_cooldown", lambda *_args: False)
    monkeypatch.setattr(redis_srv, "acquire_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(redis_srv, "release_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(redis_srv, "incr_key", lambda *_args: 1)
    caplog.set_level(logging.INFO)

    result = await telegram_search_task._scan_telegram_search_async()

    assert result == "TelegramSearch stopped because no usable Telegram session is available."
    assert fake_telegram_client.connected is False
    assert client.calls == []
    assert sleeps == []
    assert "No usable Telegram session is available for search 'api.telegram.org/bot'" in caplog.text
    assert "logged out" in caplog.text
    assert "api.telegram.org/bot" in caplog.text
    assert "next scan" in caplog.text
    assert any("logged out" in message for message in status_logs)


@pytest.mark.asyncio
async def test_empty_search_results_are_reported_as_empty(monkeypatch, caplog):
    client, _, _, sleeps = _prepare(
        monkeypatch,
        lambda _query: SimpleNamespace(chats=[], messages=[]),
    )
    caplog.set_level(logging.INFO)

    result = await telegram_search_task._scan_telegram_search_async("nothing here")

    assert result == "TelegramSearch finished. 0 matches."
    assert client.calls == [("nothing here", 100)]
    assert sleeps == [3.0]
    assert "SearchGlobal('nothing here') -> 0 messages" in caplog.text
    assert "Search 'nothing here' returned 0 messages" in caplog.text
