"""Adaptive throttling: capacity shrinks when Telegram restricts senders."""

from __future__ import annotations

from app.workers.tasks.flows import broadcast as b


class _FakeRedis:
    def __init__(self, keys):
        self._keys = keys

    def scan_iter(self, match=None, count=None):
        for key in self._keys:
            if match is None or key.startswith(match.rstrip("*")):
                yield key


def test_restricted_sender_count_counts_matching_keys(monkeypatch):
    fake = _FakeRedis([f"cooldown:bot_restricted:{i}" for i in range(273)])
    monkeypatch.setattr(b, "redis_client", fake)
    b._SENDER_HEALTH_CACHE.update(checked_at=0.0, restricted=0.0)

    assert b._restricted_sender_count(ttl_seconds=0) == 273


def test_restricted_sender_count_is_cached(monkeypatch):
    fake = _FakeRedis([f"cooldown:bot_restricted:{i}" for i in range(5)])
    monkeypatch.setattr(b, "redis_client", fake)
    b._SENDER_HEALTH_CACHE.update(checked_at=0.0, restricted=0.0)

    assert b._restricted_sender_count(ttl_seconds=60) == 5
    monkeypatch.setattr(b, "redis_client", _FakeRedis([]))
    assert b._restricted_sender_count(ttl_seconds=60) == 5  # still cached


def test_restricted_sender_count_survives_redis_errors(monkeypatch):
    class _Broken:
        def scan_iter(self, match=None, count=None):
            raise RuntimeError("redis down")

    monkeypatch.setattr(b, "redis_client", _Broken())
    b._SENDER_HEALTH_CACHE.update(checked_at=0.0, restricted=42.0)

    assert b._restricted_sender_count(ttl_seconds=0) == 42
