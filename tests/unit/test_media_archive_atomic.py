import os
import time

import pytest

from app.services import media_archive


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(media_archive.settings, "MEDIA_ARCHIVE_DIR", str(tmp_path))
    monkeypatch.setattr(media_archive.settings, "MEDIA_ARCHIVE_ENABLED", True)
    monkeypatch.setattr(media_archive.settings, "MEDIA_ARCHIVE_MAX_BYTES", 10**9)
    monkeypatch.setattr(media_archive.settings, "MEDIA_ARCHIVE_MIN_FREE_BYTES", 0)
    monkeypatch.setattr(media_archive.settings, "MEDIA_ARCHIVE_MAX_FILE_BYTES", 10**9)
    return tmp_path


def test_finalize_moves_only_complete_file(archive_dir):
    temp = archive_dir / ".key.part"
    target = archive_dir / "key"
    temp.write_bytes(b"abc")

    assert media_archive._finalize_capture(temp, target, 3) is True
    assert target.read_bytes() == b"abc"
    assert not temp.exists()


def test_finalize_rejects_short_file(archive_dir):
    temp = archive_dir / ".key.part"
    target = archive_dir / "key"
    temp.write_bytes(b"ab")

    assert media_archive._finalize_capture(temp, target, 3) is False
    assert not target.exists()
    assert not temp.exists()


def test_sweep_removes_stale_parts_and_files(archive_dir):
    stale_part = archive_dir / ".key.part"
    stale_part.write_bytes(b"partial")
    stale_final = archive_dir / "old"
    stale_final.write_bytes(b"old")
    fresh_final = archive_dir / "fresh"
    fresh_final.write_bytes(b"fresh")

    old = time.time() - 7200
    os.utime(stale_part, (old, old))
    os.utime(stale_final, (old, old))

    assert media_archive.sweep(max_age_hours=1) == 2
    assert not stale_part.exists()
    assert not stale_final.exists()
    assert fresh_final.exists()


class _DroppingStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_bytes(self, _size):
        yield b"abc"
        raise RuntimeError("connection dropped")


class _MetaResponse:
    def json(self):
        return {
            "ok": True,
            "result": {"file_size": 6, "file_path": "media/file"},
        }


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        return _MetaResponse()

    def stream(self, *_args, **_kwargs):
        return _DroppingStream()


@pytest.mark.asyncio
async def test_interrupted_download_leaves_no_final_file(archive_dir, monkeypatch):
    monkeypatch.setattr(media_archive.httpx, "AsyncClient", _FakeClient)

    result = await media_archive.capture_from_bot("token", "file-id", "key")

    assert result is None
    assert not (archive_dir / "key").exists()
    assert not (archive_dir / ".key.part").exists()
