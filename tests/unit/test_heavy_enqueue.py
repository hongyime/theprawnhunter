"""Enqueue decisions for capture-flow heavy ASR/OCR enrichment."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple] = []
        self.deleted: list[str] = []
        self.fail_set = False

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        if self.fail_set:
            raise ConnectionError("redis unavailable")
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def delete(self, key):
        self.deleted.append(key)
        self.values.pop(key, None)


class FakeTask:
    def __init__(self, name):
        self.name = name
        self.calls: list[tuple] = []
        self.fail = False

    def apply_async(self, args=None, queue=None, **kwargs):
        self.calls.append((args, queue))
        if self.fail:
            raise RuntimeError("broker unavailable")


@pytest.fixture(autouse=True)
def _clean_budget(monkeypatch):
    from app.workers.tasks.flows import heavy

    heavy.reset_enqueue_run_budget()
    monkeypatch.delenv("HEAVY_ENQUEUE_MAX_PER_RUN", raising=False)
    yield
    heavy.reset_enqueue_run_budget()


def _enable(monkeypatch, enabled=True):
    from app.workers.tasks.flows import heavy

    redis = FakeRedis()
    asr = FakeTask("heavy.transcribe_media")
    ocr = FakeTask("heavy.ocr_media")
    monkeypatch.setattr(heavy, "settings", SimpleNamespace(HEAVY_ENRICHMENT_ENABLED=enabled))
    monkeypatch.setattr(heavy, "redis_client", redis)
    monkeypatch.setattr(heavy, "transcribe_media", asr)
    monkeypatch.setattr(heavy, "ocr_media", ocr)
    return redis, asr, ocr


def test_flag_off_does_not_enqueue(monkeypatch):
    from app.workers.tasks.flows import heavy

    redis, asr, ocr = _enable(monkeypatch, enabled=False)

    assert heavy.enqueue_enrichment_for_message("voice", "msg-1") is False
    assert redis.set_calls == []
    assert asr.calls == []
    assert ocr.calls == []


@pytest.mark.parametrize("media_type", ["voice", "audio", "video_note"])
def test_audio_media_enqueues_transcribe_on_heavy_queue(monkeypatch, media_type):
    from app.workers.tasks.flows import heavy

    redis, asr, ocr = _enable(monkeypatch)

    assert heavy.enqueue_enrichment_for_message(media_type, "msg-1") is True
    assert asr.calls == [(["msg-1"], "heavy")]
    assert ocr.calls == []
    assert redis.set_calls == [("heavy:enqueued:msg-1", "1", True, 86_400)]


def test_photo_enqueues_ocr(monkeypatch):
    from app.workers.tasks.flows import heavy

    _, asr, ocr = _enable(monkeypatch)

    assert heavy.enqueue_enrichment_for_message("photo", "msg-2") is True
    assert ocr.calls == [(["msg-2"], "heavy")]
    assert asr.calls == []


def test_image_document_enqueues_ocr(monkeypatch):
    from app.workers.tasks.flows import heavy

    _, _, ocr = _enable(monkeypatch)

    assert (
        heavy.enqueue_enrichment_for_message(
            "document", "msg-3", file_meta={"mime": "image/png"}
        )
        is True
    )
    assert ocr.calls == [(["msg-3"], "heavy")]


def test_non_image_document_is_ignored(monkeypatch):
    from app.workers.tasks.flows import heavy

    redis, asr, ocr = _enable(monkeypatch)

    assert (
        heavy.enqueue_enrichment_for_message(
            "document", "msg-4", file_meta={"mime": "application/pdf"}
        )
        is False
    )
    assert redis.set_calls == []
    assert asr.calls == []
    assert ocr.calls == []


@pytest.mark.parametrize("media_type", ["text", "sticker", "callback_query", ""])
def test_unsupported_media_type_is_ignored(monkeypatch, media_type):
    from app.workers.tasks.flows import heavy

    redis, asr, ocr = _enable(monkeypatch)

    assert heavy.enqueue_enrichment_for_message(media_type, "msg-5") is False
    assert redis.set_calls == []
    assert asr.calls == []
    assert ocr.calls == []


def test_duplicate_message_id_enqueues_once(monkeypatch):
    from app.workers.tasks.flows import heavy

    redis, asr, _ = _enable(monkeypatch)

    assert heavy.enqueue_enrichment_for_message("voice", "msg-1") is True
    assert heavy.enqueue_enrichment_for_message("voice", "msg-1") is False
    assert asr.calls == [(["msg-1"], "heavy")]
    assert len(redis.set_calls) == 2


def test_redis_failure_returns_false_without_raising(monkeypatch, caplog):
    from app.workers.tasks.flows import heavy

    redis, asr, _ = _enable(monkeypatch)
    redis.fail_set = True

    with caplog.at_level(logging.WARNING, logger="flow.tasks"):
        assert heavy.enqueue_enrichment_for_message("voice", "msg-1") is False

    assert asr.calls == []
    assert "redis dedupe failed" in caplog.text


def test_broker_failure_returns_false_without_raising(monkeypatch, caplog):
    from app.workers.tasks.flows import heavy

    redis, asr, _ = _enable(monkeypatch)
    asr.fail = True

    with caplog.at_level(logging.WARNING, logger="flow.tasks"):
        assert heavy.enqueue_enrichment_for_message("voice", "msg-1") is False

    assert asr.calls == [(["msg-1"], "heavy")]
    assert redis.deleted == ["heavy:enqueued:msg-1"]
    assert "broker dispatch failed" in caplog.text


def test_per_run_cap_skips_further_messages(monkeypatch, caplog):
    from app.workers.tasks.flows import heavy

    _, asr, _ = _enable(monkeypatch)
    monkeypatch.setenv("HEAVY_ENQUEUE_MAX_PER_RUN", "2")

    with caplog.at_level(logging.INFO, logger="flow.tasks"):
        results = [
            heavy.enqueue_enrichment_for_message("voice", f"msg-{index}")
            for index in range(3)
        ]

    assert results == [True, True, False]
    assert asr.calls == [(["msg-0"], "heavy"), (["msg-1"], "heavy")]
    assert caplog.text.count("per-run cap reached") == 1


@pytest.mark.asyncio
async def test_honeypot_flow_survives_helper_exception(monkeypatch):
    from app.services import honeypot_srv
    from app.workers.tasks.flows import heavy

    payload = {
        "update_id": 4242,
        "message": {
            "message_id": 77,
            "from": {"id": 9, "username": "victim"},
            "chat": {"id": 9},
            "voice": {"file_id": "voice-file", "duration": 3},
        },
    }

    monkeypatch.setattr(
        honeypot_srv,
        "settings",
        SimpleNamespace(
            HONEYPOT_FORWARD_RAW_MESSAGES=True,
            HONEYPOT_BROADCAST_TO_TOPIC=False,
        ),
    )
    monkeypatch.setattr(
        honeypot_srv,
        "execute_async",
        AsyncMock(return_value=SimpleNamespace(data=[{"id": "stored-row-1"}])),
    )
    monkeypatch.setattr(
        honeypot_srv,
        "db",
        SimpleNamespace(table=lambda _name: MagicMock()),
    )
    monkeypatch.setattr(
        heavy,
        "enqueue_enrichment_for_message",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    result = await honeypot_srv.forward_honeypot_message(payload, "cred-1")

    assert result is not None
    assert result["media_type"] == "voice"


@pytest.mark.asyncio
async def test_scrape_call_site_survives_helper_exception(monkeypatch, caplog):
    from app.workers.tasks.flows import heavy, scrape

    monkeypatch.setattr(
        heavy,
        "enqueue_enrichment_for_message",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    with caplog.at_level(logging.WARNING, logger="flow.tasks"):
        await scrape._enqueue_heavy_for_stored_rows(
            [{"id": "stored-row-2", "media_type": "photo", "credential_id": "cred-1"}]
        )

    assert "heavy enrichment enqueue skipped" in caplog.text


def test_broadcast_call_site_survives_helper_exception(monkeypatch, caplog):
    from app.workers.tasks.flows import broadcast, heavy

    monkeypatch.setattr(
        heavy,
        "enqueue_enrichment_for_message",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    with caplog.at_level(logging.WARNING, logger="flow.tasks"):
        result = broadcast._enqueue_heavy_enrichment(
            {"id": "stored-row-3", "media_type": "voice", "credential_id": "cred-1"}
        )

    assert result is None
    assert "heavy enrichment enqueue skipped" in caplog.text
