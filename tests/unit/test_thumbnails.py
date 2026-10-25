import io
import itertools
import random
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services import thumbnails

MAX_BYTES = 64_000
MAX_DIM = 320


def _png_bytes(width: int, height: int, *, noise: bool = False, mode: str = "RGB") -> bytes:
    if noise:
        rng = random.Random(1234)
        raw = bytes(rng.getrandbits(8) for _ in range(width * height * 3))
        image = Image.frombytes("RGB", (width, height), raw)
    else:
        image = Image.new("RGB", (width, height), (20, 90, 160))
        for x in range(0, width, 3):
            for y in range(0, height, 3):
                image.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    if mode != "RGB":
        image = image.convert(mode)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeStorage:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.bucket = None
        self.uploads: list[tuple] = []

    def from_(self, bucket):
        self.bucket = bucket
        return self

    def upload(self, path, data, options=None):
        if self.fail:
            raise RuntimeError("storage down")
        self.uploads.append((path, data, options))
        return {"path": path}


class _FakeClient:
    def __init__(self, storage: _FakeStorage):
        self.storage = storage


def _decode(blob: bytes) -> Image.Image:
    return Image.open(io.BytesIO(blob))


def test_large_png_becomes_bounded_jpeg():
    source = _png_bytes(1200, 900)
    thumb = thumbnails.make_thumbnail(source)

    assert thumb is not None
    assert len(thumb) <= MAX_BYTES
    assert thumb[:2] == b"\xff\xd8"
    with _decode(thumb) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= MAX_DIM


def test_noise_image_default_budget_stays_bounded():
    source = _png_bytes(320, 320, noise=True)
    thumb = thumbnails.make_thumbnail(source)

    assert thumb is not None
    assert len(thumb) <= MAX_BYTES
    with _decode(thumb) as image:
        assert max(image.size) <= MAX_DIM


def test_noise_image_exercises_quality_and_downscale_loop():
    source = _png_bytes(320, 320, noise=True)

    thumb = thumbnails.make_thumbnail(source, max_bytes=20_000)
    assert thumb is not None
    assert len(thumb) <= 20_000
    with _decode(thumb) as image:
        assert max(image.size) < MAX_DIM

    assert thumbnails.make_thumbnail(source, max_bytes=1_000) is None


def test_undecodable_bytes_return_none():
    assert thumbnails.make_thumbnail(b"not an image at all") is None
    assert thumbnails.make_thumbnail(b"") is None
    assert thumbnails.make_thumbnail(None) is None


def test_transparent_png_is_flattened_to_rgb():
    source = _png_bytes(400, 400, mode="RGBA")
    thumb = thumbnails.make_thumbnail(source)

    assert thumb is not None
    with _decode(thumb) as image:
        assert image.mode == "RGB"


def test_thumbnail_path_convention_and_traversal_guard():
    assert thumbnails.thumbnail_path("cred-1", "msg-1") == "cred-1/msg-1.jpg"
    assert thumbnails.thumbnail_path("cred/../evil", "msg-1") is None
    assert thumbnails.thumbnail_path("", "msg-1") is None
    assert thumbnails.thumbnail_path("cred-1", None) is None


@pytest.mark.asyncio
async def test_store_thumbnail_uploads_to_private_bucket_path():
    storage = _FakeStorage()
    source = _png_bytes(400, 400, noise=True)

    path = await thumbnails.store_thumbnail(
        source, credential_id="cred-1", message_id="msg-1", client=_FakeClient(storage)
    )

    assert path == "cred-1/msg-1.jpg"
    assert storage.bucket == "evidence-thumbs"
    assert len(storage.uploads) == 1
    uploaded_path, payload, options = storage.uploads[0]
    assert uploaded_path == "cred-1/msg-1.jpg"
    assert payload[:2] == b"\xff\xd8"
    assert options == {"content-type": "image/jpeg", "upsert": "true"}


@pytest.mark.asyncio
async def test_store_thumbnail_storage_exception_returns_none():
    storage = _FakeStorage(fail=True)

    result = await thumbnails.store_thumbnail(
        _png_bytes(400, 400), credential_id="cred-1", message_id="msg-1", client=_FakeClient(storage)
    )

    assert result is None
    assert storage.uploads == []


@pytest.mark.asyncio
async def test_store_thumbnail_undecodable_never_uploads():
    storage = _FakeStorage()

    result = await thumbnails.store_thumbnail(
        b"garbage", credential_id="cred-1", message_id="msg-1", client=_FakeClient(storage)
    )

    assert result is None
    assert storage.uploads == []


@pytest.mark.asyncio
async def test_non_image_media_is_skipped():
    storage = _FakeStorage()
    client = _FakeClient(storage)

    for media_type, file_meta in (
        ("document", {"mime": "application/pdf", "file_name": "report.pdf"}),
        ("video", {"file_name": "clip.mp4"}),
        ("audio", {"file_name": "voice.ogg"}),
        ("text", {}),
    ):
        message = {"id": "msg-1", "credential_id": "cred-1", "file_meta": dict(file_meta)}
        result = await thumbnails.attach_thumbnail(
            message, media_type, _png_bytes(400, 400), client=client
        )
        assert result is None
        assert "thumb_path" not in message["file_meta"]

    assert storage.uploads == []


@pytest.mark.asyncio
async def test_photo_and_image_document_are_thumbnailed():
    storage = _FakeStorage()
    client = _FakeClient(storage)
    source = _png_bytes(400, 400)
    persisted: list[tuple] = []

    async def persist(message_id, file_meta):
        persisted.append((message_id, file_meta))

    photo = {"id": "msg-1", "credential_id": "cred-1", "file_meta": {}}
    assert await thumbnails.attach_thumbnail(photo, "photo", source, client=client, persist=persist) == (
        "cred-1/msg-1.jpg"
    )
    assert photo["file_meta"]["thumb_path"] == "cred-1/msg-1.jpg"

    image_doc = {
        "id": "msg-2",
        "credential_id": "cred-1",
        "file_meta": {"mime": "image/png", "file_name": "scan.png"},
    }
    assert await thumbnails.attach_thumbnail(image_doc, "document", source, client=client) == (
        "cred-1/msg-2.jpg"
    )
    assert image_doc["file_meta"]["thumb_path"] == "cred-1/msg-2.jpg"

    assert persisted == [("msg-1", {"thumb_path": "cred-1/msg-1.jpg"})]
    assert len(storage.uploads) == 2


@pytest.mark.asyncio
async def test_attach_thumbnail_failure_leaves_key_absent():
    message = {"id": "msg-1", "credential_id": "cred-1", "file_meta": {"mime": "image/jpeg"}}

    result = await thumbnails.attach_thumbnail(
        message, "document", b"garbage", client=_FakeClient(_FakeStorage())
    )

    assert result is None
    assert "thumb_path" not in message["file_meta"]


@pytest.mark.asyncio
async def test_attach_thumbnail_never_raises_when_store_blows_up(monkeypatch):
    message = {"id": "msg-1", "credential_id": "cred-1", "file_meta": {}}

    async def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(thumbnails, "store_thumbnail", boom)

    assert await thumbnails.attach_thumbnail(message, "photo", b"bytes") is None
    assert "thumb_path" not in message["file_meta"]


@pytest.mark.asyncio
async def test_broadcast_succeeds_when_thumbnail_hook_raises(monkeypatch):
    from app.services import broadcaster_srv

    photo_calls = []

    class _FakeBot:
        async def send_photo(self, **kwargs):
            photo_calls.append(kwargs)
            return SimpleNamespace(message_id=77)

        async def send_message(self, **_kwargs):
            return SimpleNamespace(message_id=78)

    monkeypatch.setattr(
        broadcaster_srv,
        "settings",
        SimpleNamespace(
            bot_tokens=["123:ABC"],
            AUTO_ARCHIVE_MEDIA=False,
            MONITOR_GROUP_ID=-100123,
            REDIS_URL="redis://localhost:6379/0",
            TELEGRAM_LOG_MIN_INTERVAL_SECONDS=2.0,
            TELEGRAM_LOG_FAILURE_WARN_INTERVAL_SECONDS=60,
        ),
    )
    service = broadcaster_srv.BroadcasterService()
    service._cycle = itertools.cycle(service._pool)

    async def no_wait():
        return None

    async def fake_download(_file_meta, _credential_id):
        return _png_bytes(400, 400)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("thumbnail explosion")

    monkeypatch.setattr(service, "_wait_for_rate_limit", no_wait)
    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: _FakeBot())
    monkeypatch.setattr(service, "_download_media_bytes", fake_download)
    monkeypatch.setattr(thumbnails, "attach_thumbnail", boom)

    result = await service.send_message(
        -100123,
        4,
        {
            "id": "row-1",
            "content": "photo",
            "sender_name": "tester",
            "media_type": "photo",
            "telegram_msg_id": 88,
            "credential_id": "cred-1",
            "file_meta": {"file_id": "file-1"},
        },
    )

    assert result == 77
    assert len(photo_calls) == 1


@pytest.mark.asyncio
async def test_broadcast_merges_thumb_path_on_success(monkeypatch):
    from app.services import broadcaster_srv

    class _FakeBot:
        async def send_photo(self, **kwargs):
            return SimpleNamespace(message_id=77)

        async def send_message(self, **_kwargs):
            return SimpleNamespace(message_id=78)

    monkeypatch.setattr(
        broadcaster_srv,
        "settings",
        SimpleNamespace(
            bot_tokens=["123:ABC"],
            AUTO_ARCHIVE_MEDIA=False,
            MONITOR_GROUP_ID=-100123,
            REDIS_URL="redis://localhost:6379/0",
            TELEGRAM_LOG_MIN_INTERVAL_SECONDS=2.0,
            TELEGRAM_LOG_FAILURE_WARN_INTERVAL_SECONDS=60,
        ),
    )
    service = broadcaster_srv.BroadcasterService()
    service._cycle = itertools.cycle(service._pool)

    async def no_wait():
        return None

    async def fake_download(_file_meta, _credential_id):
        return _png_bytes(400, 400)

    attach_calls = []

    async def fake_attach(msg_obj, media_type, media_bytes, *, persist=None):
        attach_calls.append((media_type, msg_obj.get("id")))
        msg_obj.setdefault("file_meta", {})["thumb_path"] = "cred-1/row-1.jpg"
        return "cred-1/row-1.jpg"

    monkeypatch.setattr(service, "_wait_for_rate_limit", no_wait)
    monkeypatch.setattr(service, "_get_bot_instance", lambda _token: _FakeBot())
    monkeypatch.setattr(service, "_download_media_bytes", fake_download)
    monkeypatch.setattr(thumbnails, "attach_thumbnail", fake_attach)

    message = {
        "id": "row-1",
        "content": "photo",
        "sender_name": "tester",
        "media_type": "photo",
        "telegram_msg_id": 88,
        "credential_id": "cred-1",
        "file_meta": {"file_id": "file-1"},
    }

    assert await service.send_message(-100123, 4, message) == 77
    assert attach_calls == [("photo", "row-1")]
    assert message["file_meta"]["thumb_path"] == "cred-1/row-1.jpg"
