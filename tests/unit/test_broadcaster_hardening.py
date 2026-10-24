import asyncio
import importlib
import itertools
import logging
import mimetypes
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, TimedOut


class _FakeBot:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []
        self.document_calls = []
        self.photo_calls = []
        self.video_calls = []
        self.audio_calls = []
        self.closed_topics = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error

    async def send_document(self, **kwargs):
        self.document_calls.append(kwargs)
        if self.error:
            raise self.error

    async def send_photo(self, **kwargs):
        self.photo_calls.append(kwargs)
        if self.error:
            raise self.error

    async def send_video(self, **kwargs):
        self.video_calls.append(kwargs)
        if self.error:
            raise self.error

    async def send_audio(self, **kwargs):
        self.audio_calls.append(kwargs)
        if self.error:
            raise self.error

    async def close_forum_topic(self, **kwargs):
        self.closed_topics.append(kwargs)
        if self.error:
            raise self.error


def _settings(bot_tokens=None, auto_archive=False):
    return SimpleNamespace(
        bot_tokens=bot_tokens or ["123:ABC"],
        AUTO_ARCHIVE_MEDIA=auto_archive,
        MONITOR_GROUP_ID=-100123,
        REDIS_URL="redis://localhost:6379/0",
        TELEGRAM_LOG_MIN_INTERVAL_SECONDS=2.0,
        TELEGRAM_LOG_FAILURE_WARN_INTERVAL_SECONDS=60,
    )


def test_android_package_mime_type_is_registered():
    import app.services.broadcaster_srv  # noqa: F401

    assert mimetypes.guess_type("payload.apk", strict=False)[0] == (
        "application/vnd.android.package-archive"
    )


def test_broadcast_rate_limit_sleep_reads_env_override(monkeypatch):
    from app.core import constants
    from app.services import broadcaster_srv

    try:
        with monkeypatch.context() as env:
            env.setenv("BROADCAST_RATE_LIMIT_SLEEP_S", "3.75")
            importlib.reload(constants)

            service = broadcaster_srv.BroadcasterService()

            assert service._min_delay == 3.75
    finally:
        importlib.reload(constants)


@pytest.mark.asyncio
async def test_send_document_targets_the_requested_topic():
    from app.services.broadcaster_srv import BroadcasterService

    service = BroadcasterService.__new__(BroadcasterService)
    service._pool = [{"type": "bot", "id": "123:ABC"}]
    service._cycle = itertools.cycle(service._pool)
    service._failed_tokens = set()
    service._flood_wait_hits = 0
    service._wait_for_rate_limit = AsyncMock()
    bot = _FakeBot()
    service._get_bot_instance = lambda _token: bot

    result = await service.send_document(-100123, 456, b'{"evidence": true}', "evidence.json")

    assert result is None
    assert bot.document_calls == [
        {
            "chat_id": -100123,
            "message_thread_id": 456,
            "document": b'{"evidence": true}',
            "filename": "evidence.json",
            "disable_content_type_detection": False,
        }
    ]


@pytest.mark.asyncio
async def test_resolve_chat_id_falls_back_to_credential_lookup(monkeypatch):
    from app.services import broadcaster_srv

    class _FakeQuery:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

    class _FakeDb:
        def __init__(self):
            self.table_name = None

        def table(self, table_name):
            self.table_name = table_name
            return _FakeQuery()

    fake_db = _FakeDb()
    monkeypatch.setitem(sys.modules, "app.core.database", SimpleNamespace(db=fake_db))

    async def fake_async_execute(_query):
        return SimpleNamespace(data=[{"chat_id": -100987654}])

    monkeypatch.setattr(broadcaster_srv, "_async_execute", fake_async_execute)
    monkeypatch.setattr(broadcaster_srv, "settings", _settings())

    service = broadcaster_srv.BroadcasterService()

    chat_id = await service._resolve_chat_id({"credential_id": "cred-1"})

    assert chat_id == -100987654
    assert fake_db.table_name == "discovered_credentials"


@pytest.mark.asyncio
async def test_resolve_chat_id_prefers_joined_bot_username(monkeypatch):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings())
    service = broadcaster_srv.BroadcasterService()

    chat_id = await service._resolve_chat_id(
        {
            "chat_id": 8940899601,
            "discovered_credentials": {
                "chat_id": 8940899601,
                "meta": {"bot_username": "ItsWatermarkBot"},
            },
        }
    )

    assert chat_id == "@ItsWatermarkBot"


@pytest.mark.asyncio
async def test_resolve_chat_id_prefers_db_bot_username(monkeypatch):
    from app.services import broadcaster_srv

    class _FakeQuery:
        selected = None

        def select(self, columns, *_args, **_kwargs):
            self.selected = columns
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

    class _FakeDb:
        def __init__(self):
            self.query = _FakeQuery()

        def table(self, _table_name):
            return self.query

    fake_db = _FakeDb()
    monkeypatch.setitem(sys.modules, "app.core.database", SimpleNamespace(db=fake_db))

    async def fake_async_execute(_query):
        return SimpleNamespace(
            data=[{"chat_id": 8940899601, "meta": {"bot_username": "@ItsWatermarkBot"}}]
        )

    monkeypatch.setattr(broadcaster_srv, "_async_execute", fake_async_execute)
    monkeypatch.setattr(broadcaster_srv, "settings", _settings())

    service = broadcaster_srv.BroadcasterService()

    chat_id = await service._resolve_chat_id({"credential_id": "cred-1"})

    assert chat_id == "@ItsWatermarkBot"
    assert fake_db.query.selected == "chat_id, meta"


@pytest.mark.asyncio
async def test_schedule_auto_archive_tracks_and_logs_task_failures(monkeypatch, caplog):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings())
    service = broadcaster_srv.BroadcasterService()

    async def crash_auto_archive(*_args, **_kwargs):
        raise RuntimeError("archive exploded")

    monkeypatch.setattr(service, "_auto_archive_media", crash_auto_archive)
    caplog.set_level(logging.ERROR, logger="broadcaster")

    service._schedule_auto_archive(-100123, 12, {"telegram_msg_id": 42}, 42)
    assert len(service._archive_tasks) == 1

    task = next(iter(service._archive_tasks))
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert service._archive_tasks == set()
    assert "Auto-archive task crashed for msg=42" in caplog.text


@pytest.mark.asyncio
async def test_send_message_marks_forbidden_bot_failed(monkeypatch):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings(bot_tokens=["123:ABC"]))
    service = broadcaster_srv.BroadcasterService()

    async def no_wait():
        return None

    fake_bot = _FakeBot(error=Forbidden("bot was kicked"))
    monkeypatch.setattr(service, "_wait_for_rate_limit", no_wait)
    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: fake_bot)

    with pytest.raises(broadcaster_srv.BroadcastSendError) as exc_info:
        await service.send_message(
            -100123,
            1,
            {"content": "hello", "sender_name": "tester", "media_type": "text", "telegram_msg_id": 77},
        )

    assert "123:ABC" in service._failed_tokens
    assert exc_info.value.reason == "forbidden"


@pytest.mark.asyncio
async def test_close_topic_uses_forum_close_without_deleting(monkeypatch):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings(bot_tokens=["123:ABC"]))
    service = broadcaster_srv.BroadcasterService()
    fake_bot = _FakeBot()

    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: fake_bot)

    closed = await service.close_topic(-100123, 44)

    assert closed is True
    assert fake_bot.closed_topics == [{"chat_id": -100123, "message_thread_id": 44}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_text",
    [
        "Bad Request: TOPIC_CLOSED",
        "Topic_not_modified",
    ],
)
async def test_close_topic_treats_already_closed_as_success(monkeypatch, error_text):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings(bot_tokens=["123:ABC"]))
    service = broadcaster_srv.BroadcasterService()
    fake_bot = _FakeBot(error=Exception(error_text))

    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: fake_bot)

    closed = await service.close_topic(-100123, 44)

    assert closed is True
    assert fake_bot.closed_topics == [{"chat_id": -100123, "message_thread_id": 44}]


@pytest.mark.asyncio
async def test_send_message_awaits_auto_archive_for_supported_media(monkeypatch):
    from app.services import broadcaster_srv

    monkeypatch.setattr(
        broadcaster_srv,
        "settings",
        _settings(bot_tokens=["123:ABC"], auto_archive=True),
    )
    service = broadcaster_srv.BroadcasterService()
    archived = []

    async def no_wait():
        return None

    async def fake_archive(*args):
        archived.append(args)
        return SimpleNamespace(ok=True, code="ok", detail="")

    monkeypatch.setattr(service, "_wait_for_rate_limit", no_wait)
    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: _FakeBot())
    monkeypatch.setattr(service, "_auto_archive_media", fake_archive)

    await service.send_message(
        -100123,
        4,
        {
            "id": "row-1",
            "content": "photo",
            "sender_name": "tester",
            "media_type": "photo",
            "telegram_msg_id": 88,
            "credential_id": "cred-1",
        },
    )

    assert archived == [(-100123, 4, {
        "id": "row-1",
        "content": "photo",
        "sender_name": "tester",
        "media_type": "photo",
        "telegram_msg_id": 88,
        "credential_id": "cred-1",
    }, 88)]


@pytest.mark.asyncio
async def test_send_document_preserves_original_filename(monkeypatch):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings(bot_tokens=["123:ABC"]))
    service = broadcaster_srv.BroadcasterService()
    fake_bot = _FakeBot()

    async def no_wait():
        return None

    async def fake_download(_file_meta, _credential_id):
        return b"apk-bytes"

    monkeypatch.setattr(service, "_wait_for_rate_limit", no_wait)
    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: fake_bot)
    monkeypatch.setattr(service, "_download_media_bytes", fake_download)

    await service.send_message(
        -100123,
        4,
        {
            "id": "row-1",
            "content": "apk",
            "sender_name": "tester",
            "media_type": "document",
            "telegram_msg_id": 88,
            "credential_id": "cred-1",
            "file_meta": {
                "file_id": "file-1",
                "file_name": "NetPlus Pro.apk",
                "mime": "application/octet-stream",
            },
        },
    )

    assert len(fake_bot.calls) == 1
    assert fake_bot.calls[0]["text"] == "apk"
    assert len(fake_bot.document_calls) == 1
    sent = fake_bot.document_calls[0]
    assert sent["document"] == b"apk-bytes"
    assert sent["filename"] == "NetPlus Pro.apk"
    assert sent["disable_content_type_detection"] is False


@pytest.mark.asyncio
async def test_media_upload_failure_does_not_fallback_to_text(monkeypatch):
    from app.services import broadcaster_srv

    monkeypatch.setattr(
        broadcaster_srv,
        "settings",
        _settings(bot_tokens=["123:ABC"], auto_archive=False),
    )
    service = broadcaster_srv.BroadcasterService()
    fake_bot = _FakeBot(error=BadRequest("file is too big"))

    async def no_wait():
        return None

    async def fake_download(_file_meta, _credential_id):
        return b"apk-bytes"

    monkeypatch.setattr(service, "_wait_for_rate_limit", no_wait)
    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: fake_bot)
    monkeypatch.setattr(service, "_download_media_bytes", fake_download)

    with pytest.raises(broadcaster_srv.BroadcastSendError) as exc_info:
        await service.send_message(
            -100123,
            4,
            {
                "id": "row-1",
                "content": "apk",
                "sender_name": "tester",
                "media_type": "document",
                "telegram_msg_id": 88,
                "credential_id": "cred-1",
                "file_meta": {"file_id": "file-1", "file_name": "payload.apk"},
            },
        )

    assert exc_info.value.reason == "bad_request"
    assert fake_bot.calls == []
    assert len(fake_bot.document_calls) == 1


@pytest.mark.asyncio
async def test_media_without_download_bytes_uses_archive_path(monkeypatch):
    from app.services import broadcaster_srv

    monkeypatch.setattr(
        broadcaster_srv,
        "settings",
        _settings(bot_tokens=["123:ABC"], auto_archive=True),
    )
    service = broadcaster_srv.BroadcasterService()
    fake_bot = _FakeBot()
    archived = []

    async def no_wait():
        return None

    async def no_download(_file_meta, _credential_id):
        return None

    async def fake_archive(*args):
        archived.append(args)
        return SimpleNamespace(ok=True, code="ok", detail="")

    monkeypatch.setattr(service, "_wait_for_rate_limit", no_wait)
    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: fake_bot)
    monkeypatch.setattr(service, "_download_media_bytes", no_download)
    monkeypatch.setattr(service, "_auto_archive_media", fake_archive)

    await service.send_message(
        -100123,
        4,
        {
            "id": "row-1",
            "content": "apk",
            "sender_name": "tester",
            "media_type": "document",
            "telegram_msg_id": 88,
            "credential_id": "cred-1",
            "file_meta": {"file_id": "file-1", "file_name": "payload.apk"},
        },
    )

    assert fake_bot.calls == []
    assert fake_bot.document_calls == []
    assert archived


def test_broadcast_exception_classifier_maps_timeout_and_topic_missing():
    from app.services.broadcaster_srv import _classify_broadcast_exception

    timeout = _classify_broadcast_exception(TimedOut("slow"))
    topic = _classify_broadcast_exception(BadRequest("Message thread not found"))

    assert timeout.reason == "timeout"
    assert timeout.retryable is True
    assert topic.reason == "topic_missing"
    assert topic.retryable is True


@pytest.mark.asyncio
async def test_send_log_respects_rate_limit(monkeypatch):
    from app.services import broadcaster_srv

    monkeypatch.setattr(broadcaster_srv, "settings", _settings(bot_tokens=["123:ABC"]))
    service = broadcaster_srv.BroadcasterService()
    fake_bot = _FakeBot()

    monkeypatch.setattr(service, "_acquire_system_log_slot", lambda: False)
    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: fake_bot)

    sent = await service.send_log("suppressed")

    assert sent is False
    assert fake_bot.calls == []
