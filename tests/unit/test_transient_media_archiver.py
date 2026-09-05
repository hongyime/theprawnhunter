import os
import sys
import time
from types import SimpleNamespace

import pytest

from app.services import user_agent_srv
from app.services.user_agent_srv import UserAgentService


class _FakeArchiveClient:
    def __init__(self, *, send_failures_before_success: int = 0, size: int = 512):
        self.send_failures_before_success = send_failures_before_success
        self.download_calls = []
        self.send_calls = []
        self.message = SimpleNamespace(
            media=object(),
            file=SimpleNamespace(name="evidence.pdf", size=size),
        )

    async def get_messages(self, entity_or_chat_id, ids):
        self.get_messages_call = (entity_or_chat_id, ids)
        return self.message

    async def download_media(self, message, file):
        self.download_calls.append((message, file))
        return file

    async def send_file(self, target_chat_id, temp_path, caption="", reply_to=None):
        self.send_calls.append(
            {
                "target_chat_id": target_chat_id,
                "temp_path": temp_path,
                "caption": caption,
                "reply_to": reply_to,
            }
        )
        if len(self.send_calls) <= self.send_failures_before_success:
            raise RuntimeError("upload failed")


class _MissingEntityThenUsernameClient(_FakeArchiveClient):
    def __init__(self, *, missing_source, username_source="@ItsWatermarkBot"):
        super().__init__()
        self.missing_source = missing_source
        self.username_source = username_source
        self.get_message_calls = []

    async def get_messages(self, entity_or_chat_id, ids):
        self.get_message_calls.append((entity_or_chat_id, ids))
        if entity_or_chat_id == self.missing_source:
            raise ValueError(
                f"Could not find the input entity for PeerUser(user_id={entity_or_chat_id})"
            )
        if entity_or_chat_id == self.username_source:
            return self.message
        raise AssertionError(f"unexpected source {entity_or_chat_id}")


class _FakeCredentialQuery:
    def __init__(self):
        self.selected = None
        self.or_filters = None

    def select(self, columns):
        self.selected = columns
        return self

    def or_(self, filters):
        self.or_filters = filters
        return self

    def limit(self, _size):
        return self


class _FakeCredentialDb:
    def __init__(self):
        self.query = _FakeCredentialQuery()

    def table(self, _table_name):
        return self.query


def _service_with_client(client):
    service = UserAgentService()
    service.client = client

    async def start():
        return True

    async def disconnect():
        service.disconnected = True

    service.start = start
    service._disconnect = disconnect
    return service


def _patch_archive_credential_lookup(monkeypatch, rows):
    fake_db = _FakeCredentialDb()
    monkeypatch.setitem(sys.modules, "app.core.database", SimpleNamespace(db=fake_db))

    async def fake_async_execute(_query):
        return SimpleNamespace(data=rows)

    monkeypatch.setattr(user_agent_srv, "_async_execute", fake_async_execute)
    return fake_db


@pytest.mark.asyncio
async def test_archive_media_transiently_downloads_reuploads_and_cleans_up(monkeypatch):
    client = _FakeArchiveClient()
    service = _service_with_client(client)
    existing_paths = set()
    removed_paths = []

    def fake_exists(path):
        return path in existing_paths

    def fake_remove(path):
        removed_paths.append(path)
        existing_paths.discard(path)

    original_download = client.download_media

    async def tracked_download(message, file):
        existing_paths.add(file)
        return await original_download(message, file)

    client.download_media = tracked_download
    monkeypatch.setattr(user_agent_srv.os.path, "exists", fake_exists)
    monkeypatch.setattr(user_agent_srv.os, "remove", fake_remove)

    result = await service.archive_media_transiently(
        -100123,
        42,
        target_chat_id=-100999,
        topic_id=77,
        caption="A" * 1100,
    )

    assert result.ok is True
    assert result.code == "ok"
    assert bool(result) is True
    assert result.size_bytes == 512
    assert client.get_messages_call == (-100123, 42)
    assert len(client.send_calls) == 1
    sent = client.send_calls[0]
    assert sent["target_chat_id"] == -100999
    assert sent["reply_to"] == 77
    assert sent["caption"] == "A" * 1024
    assert sent["temp_path"].startswith("/tmp/archive_")
    assert sent["temp_path"].endswith("_42_evidence.pdf")
    assert removed_paths == [sent["temp_path"]]
    assert service.disconnected is True


@pytest.mark.asyncio
async def test_archive_media_transiently_retries_missing_entity_as_username(monkeypatch):
    client = _MissingEntityThenUsernameClient(missing_source=8940899601)
    service = _service_with_client(client)
    existing_paths = set()
    removed_paths = []
    fake_db = _patch_archive_credential_lookup(
        monkeypatch,
        [{"meta": {"bot_username": "ItsWatermarkBot"}}],
    )

    def fake_exists(path):
        return path in existing_paths

    def fake_remove(path):
        removed_paths.append(path)
        existing_paths.discard(path)

    original_download = client.download_media

    async def tracked_download(message, file):
        existing_paths.add(file)
        return await original_download(message, file)

    client.download_media = tracked_download
    monkeypatch.setattr(user_agent_srv.os.path, "exists", fake_exists)
    monkeypatch.setattr(user_agent_srv.os, "remove", fake_remove)

    result = await service.archive_media_transiently(
        8940899601,
        47,
        target_chat_id=-100999,
        caption="Archived Attachment",
    )

    assert result.ok is True
    assert result.code == "ok"
    assert client.get_message_calls == [(8940899601, 47), ("@ItsWatermarkBot", 47)]
    assert fake_db.query.selected == "meta"
    assert fake_db.query.or_filters == "chat_id.eq.8940899601"
    assert len(client.download_calls) == 1
    assert len(client.send_calls) == 1
    assert removed_paths == [client.send_calls[0]["temp_path"]]


@pytest.mark.asyncio
async def test_archive_media_transiently_returns_missing_access_hash_without_username(monkeypatch):
    client = _MissingEntityThenUsernameClient(missing_source=8940899601)
    service = _service_with_client(client)
    fake_db = _patch_archive_credential_lookup(monkeypatch, [{"meta": {}}])

    result = await service.archive_media_transiently(
        8940899601,
        48,
        target_chat_id=-100999,
    )

    assert result.ok is False
    assert result.code == "missing_access_hash"
    assert "Missing Telethon access_hash" in result.detail
    assert client.get_message_calls == [(8940899601, 48)]
    assert fake_db.query.or_filters == "chat_id.eq.8940899601"
    assert client.download_calls == []
    assert client.send_calls == []


@pytest.mark.asyncio
async def test_archive_media_transiently_cleans_up_when_send_file_fails(monkeypatch):
    client = _FakeArchiveClient(send_failures_before_success=2)
    service = _service_with_client(client)
    existing_paths = set()
    removed_paths = []

    def fake_exists(path):
        return path in existing_paths

    def fake_remove(path):
        removed_paths.append(path)
        existing_paths.discard(path)

    original_download = client.download_media

    async def tracked_download(message, file):
        existing_paths.add(file)
        return await original_download(message, file)

    client.download_media = tracked_download
    monkeypatch.setattr(user_agent_srv.os.path, "exists", fake_exists)
    monkeypatch.setattr(user_agent_srv.os, "remove", fake_remove)
    monkeypatch.setattr(user_agent_srv.settings, "ARCHIVE_RETRY_ATTEMPTS", 2)

    result = await service.archive_media_transiently(
        -100123,
        43,
        target_chat_id=-100999,
        topic_id=1,
        caption="Archived Attachment",
    )

    assert result.ok is False
    assert result.code == "upload_failed"
    assert bool(result) is False
    assert len(client.send_calls) == 2
    sent = client.send_calls[0]
    assert sent["reply_to"] is None
    assert sent["temp_path"].endswith("_43_evidence.pdf")
    assert removed_paths == [sent["temp_path"]]
    assert service.disconnected is True


@pytest.mark.asyncio
async def test_archive_media_transiently_retries_transient_upload_failure(monkeypatch):
    client = _FakeArchiveClient(send_failures_before_success=1)
    service = _service_with_client(client)
    existing_paths = set()
    removed_paths = []

    def fake_exists(path):
        return path in existing_paths

    def fake_remove(path):
        removed_paths.append(path)
        existing_paths.discard(path)

    original_download = client.download_media

    async def tracked_download(message, file):
        existing_paths.add(file)
        return await original_download(message, file)

    async def no_sleep(_delay):
        return None

    client.download_media = tracked_download
    monkeypatch.setattr(user_agent_srv.os.path, "exists", fake_exists)
    monkeypatch.setattr(user_agent_srv.os, "remove", fake_remove)
    monkeypatch.setattr(user_agent_srv.settings, "ARCHIVE_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(user_agent_srv.asyncio, "sleep", no_sleep)

    result = await service.archive_media_transiently(
        -100123,
        44,
        target_chat_id=-100999,
        topic_id=2,
        caption="Archived Attachment",
    )

    assert result.ok is True
    assert result.code == "ok"
    assert len(client.send_calls) == 2
    assert removed_paths == [client.send_calls[0]["temp_path"]]


@pytest.mark.asyncio
async def test_archive_media_transiently_rejects_oversized_payload_before_download(monkeypatch):
    client = _FakeArchiveClient(size=2 * 1024 * 1024 * 1024)
    service = _service_with_client(client)

    result = await service.archive_media_transiently(
        -100123,
        45,
        target_chat_id=-100999,
    )

    assert result.ok is False
    assert result.code == "too_large"
    assert result.size_bytes == 2 * 1024 * 1024 * 1024
    assert "limit is 1024 MB" in result.detail
    assert client.download_calls == []
    assert client.send_calls == []


@pytest.mark.asyncio
async def test_archive_media_transiently_returns_timeout_on_download_timeout(monkeypatch):
    client = _FakeArchiveClient()
    service = _service_with_client(client)

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(user_agent_srv.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(user_agent_srv.settings, "ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS", 1)

    result = await service.archive_media_transiently(
        -100123,
        46,
        target_chat_id=-100999,
    )

    assert result.ok is False
    assert result.code == "timeout"
    assert "Download exceeded 1s" in result.detail
    assert client.send_calls == []


def test_cleanup_stale_tmp_archives_removes_only_old_archive_files(monkeypatch, tmp_path):
    old_archive = tmp_path / "archive_old.bin"
    fresh_archive = tmp_path / "archive_fresh.bin"
    unrelated = tmp_path / "other.bin"
    old_archive.write_text("old")
    fresh_archive.write_text("fresh")
    unrelated.write_text("other")

    old_time = time.time() - 3600
    os.utime(old_archive, (old_time, old_time))

    service = UserAgentService()
    monkeypatch.setattr(user_agent_srv, "ARCHIVE_TMP_DIR", str(tmp_path))
    monkeypatch.setattr(user_agent_srv.settings, "ARCHIVE_STALE_TMP_MAX_AGE_SECONDS", 1800)

    removed = service._cleanup_stale_tmp_archives()

    assert removed == 1
    assert not old_archive.exists()
    assert fresh_archive.exists()
    assert unrelated.exists()
