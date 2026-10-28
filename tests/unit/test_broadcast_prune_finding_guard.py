from types import SimpleNamespace

import pytest


class _Query:
    def __init__(self, table: str):
        self.table = table
        self.action = None
        self.ids: list[str] | None = None
        self.terminal = False

    def select(self, *_args, **_kwargs):
        self.action = "select"
        return self

    def delete(self):
        self.action = "delete"
        return self

    def update(self, _payload):
        self.action = "update"
        return self

    def eq(self, key, _value):
        if key == "broadcast_error->>retryable":
            self.terminal = True
        return self

    def is_(self, *_args, **_kwargs):
        return self

    def lt(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def in_(self, _key, values):
        self.ids = list(values)
        return self


class _Db:
    def table(self, table: str) -> _Query:
        return _Query(table)


@pytest.mark.asyncio
async def test_prune_keeps_messages_referenced_by_findings(monkeypatch):
    from app.workers.tasks.flows import broadcast

    deleted: list[str] = []

    async def fake_execute(query: _Query):
        if (
            query.table == "exfiltrated_messages"
            and query.action == "select"
            and query.terminal
        ):
            return SimpleNamespace(data=[])
        if query.table == "exfiltrated_messages" and query.action == "select":
            return SimpleNamespace(data=[{"id": "m1"}, {"id": "m2"}])
        if query.table == "finding_evidence":
            return SimpleNamespace(data=[{"source_id": "m1"}])
        if query.table == "exfiltrated_messages" and query.action == "delete":
            deleted.extend(query.ids or [])
            return SimpleNamespace(data=[])
        raise AssertionError((query.table, query.action))

    monkeypatch.setattr(broadcast, "db", _Db())
    monkeypatch.setattr(broadcast, "async_execute", fake_execute)

    result = await broadcast._prune_failed_broadcasts_async()

    assert result == {"deleted": 1, "marked_terminal": 0}
    assert deleted == ["m2"]
