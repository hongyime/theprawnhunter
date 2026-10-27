"""Bounded capture-time media cache."""

from __future__ import annotations

import os
import time

import pytest

from app.core.config import settings
from app.services import media_archive


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ARCHIVE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "MEDIA_ARCHIVE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "MEDIA_ARCHIVE_MAX_BYTES", 1024, raising=False)
    monkeypatch.setattr(settings, "MEDIA_ARCHIVE_MIN_FREE_BYTES", 0, raising=False)
    media_archive._CAPACITY_CACHE.update(checked_at=0.0, used=0.0)
    return tmp_path


def test_key_is_deterministic_and_file_id_sensitive():
    a = media_archive.key_for("cred-1", 42, "AgAC")
    b = media_archive.key_for("cred-1", 42, "AgAC")
    c = media_archive.key_for("cred-1", 42, "Other")
    assert a == b
    assert a != c
    assert a.startswith("cred-1/42-")


def test_capacity_stops_at_size_limit(cache_dir, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ARCHIVE_MAX_BYTES", 10, raising=False)
    media_archive._CAPACITY_CACHE.update(checked_at=0.0, used=0.0)
    target = cache_dir / "cred-1" / "1-abc"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 20)

    ok, reason = media_archive.capacity_status()
    assert ok is False
    assert "limit" in reason


def test_capacity_disabled_flag(cache_dir, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ARCHIVE_ENABLED", False, raising=False)
    ok, reason = media_archive.capacity_status()
    assert ok is False
    assert reason == "disabled"


def test_delete_removes_file(cache_dir):
    key = media_archive.key_for("cred-1", 7, "f")
    path = media_archive.path_for(key)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")

    assert media_archive.delete(key) is True
    assert not path.exists()


def test_sweep_only_removes_expired_files(cache_dir):
    old_key = media_archive.key_for("c", 1, "a")
    new_key = media_archive.key_for("c", 2, "b")
    for key in (old_key, new_key):
        path = media_archive.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    old_path = media_archive.path_for(old_key)
    stale = time.time() - 3 * 3600
    os.utime(old_path, (stale, stale))

    removed = media_archive.sweep(max_age_hours=1)
    assert removed == 1
    assert not old_path.exists()
    assert media_archive.path_for(new_key).exists()


class _FakeMessage:
    def __init__(self, media=True):
        self.media = object() if media else None


class _FakeClient:
    def __init__(self, message, payload=b"video-bytes"):
        self._message = message
        self._payload = payload
        self.downloaded = []

    async def get_messages(self, entity, ids):
        return self._message

    async def download_media(self, message, file):
        from pathlib import Path

        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._payload)
        self.downloaded.append(path)
        return str(path)


@pytest.mark.asyncio
async def test_capture_from_chat_writes_file(cache_dir):
    client = _FakeClient(_FakeMessage())
    key = media_archive.key_for("c1", 99, "")
    path = await media_archive.capture_from_chat(client, "@bot", 99, key)

    assert path is not None
    assert path.exists()
    assert path.read_bytes() == b"video-bytes"


@pytest.mark.asyncio
async def test_capture_from_chat_skips_message_without_media(cache_dir):
    client = _FakeClient(_FakeMessage(media=False))
    key = media_archive.key_for("c1", 100, "")
    assert await media_archive.capture_from_chat(client, "@bot", 100, key) is None


@pytest.mark.asyncio
async def test_capture_from_chat_respects_capacity(cache_dir, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ARCHIVE_ENABLED", False, raising=False)
    client = _FakeClient(_FakeMessage())
    key = media_archive.key_for("c1", 101, "")
    assert await media_archive.capture_from_chat(client, "@bot", 101, key) is None
    assert client.downloaded == []
