import pytest
from types import SimpleNamespace

from app.workers.tasks.flows import watchlist_backfill as task


class _Redis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.mark.asyncio
async def test_advances_the_cursor_and_counts_hits(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(task, "redis_client", redis)
    rows = [
        {"id": "a", "credential_id": "c1", "content": "telegram leak", "created_at": "2026-10-26T10:00:00+00:00"},
        {"id": "b", "credential_id": "c1", "content": "", "created_at": "2026-10-26T09:00:00+00:00"},
    ]

    async def fake_execute(_query):
        return SimpleNamespace(data=rows)

    monkeypatch.setattr(task, "execute_async", fake_execute)

    async def fake_evaluate(content, *, message_id, credential_id=None, **_):
        return [{"rule_id": "r1"}] if message_id == "a" else []

    import app.services.watchlists as watchlists

    monkeypatch.setattr(watchlists, "evaluate", fake_evaluate)

    result = await task._backfill_watchlists_async(100, False)

    assert result == "watchlist_backfill:scanned=1,hits=1"
    assert redis.store[task.CURSOR_KEY] == "2026-10-26T09:00:00+00:00"


@pytest.mark.asyncio
async def test_reports_done_when_no_rows_remain(monkeypatch):
    monkeypatch.setattr(task, "redis_client", _Redis())

    async def empty(_query):
        return SimpleNamespace(data=[])

    monkeypatch.setattr(task, "execute_async", empty)
    assert await task._backfill_watchlists_async(100, False) == "watchlist_backfill:done"


@pytest.mark.asyncio
async def test_evaluation_errors_do_not_stop_the_run(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(task, "redis_client", redis)

    async def fake_execute(_query):
        return SimpleNamespace(data=[{"id": "a", "credential_id": None, "content": "x", "created_at": "2026-01-01T00:00:00+00:00"}])

    monkeypatch.setattr(task, "execute_async", fake_execute)

    import app.services.watchlists as watchlists

    async def boom(*args, **kwargs):
        raise RuntimeError("rules down")

    monkeypatch.setattr(watchlists, "evaluate", boom)

    assert await task._backfill_watchlists_async(10, False) == "watchlist_backfill:scanned=1,hits=0"
