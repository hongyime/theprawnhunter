"""Unit tests for the operator-only bot ``/watchlist`` command.

No live database or Telegram: the service-role client and the deep-link
resolver are replaced with fakes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telegram.constants import ParseMode

from app.services._bot_listener import watchlist_commands

RULE_A = "aaaaaaaa-1111-1111-1111-111111111111"
RULE_B = "bbbbbbbb-2222-2222-2222-222222222222"
RULE_C = "cccccccc-3333-3333-3333-333333333333"
CRED_A = "12345678-aaaa-bbbb-cccc-dddddddddddd"
MESSAGE_1 = "11111111-aaaa-bbbb-cccc-dddddddddddd"
MESSAGE_2 = "22222222-aaaa-bbbb-cccc-dddddddddddd"


def _rule(
    identifier: str,
    name: str = "Rule",
    pattern: str = "needle",
    *,
    is_regex: bool = False,
    credential_id: str | None = None,
    enabled: bool = True,
    cooldown_minutes: int = 60,
) -> dict:
    return {
        "id": identifier,
        "name": name,
        "pattern": pattern,
        "is_regex": is_regex,
        "credential_id": credential_id,
        "enabled": enabled,
        "cooldown_minutes": cooldown_minutes,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _hit(
    *,
    rule_id: str = RULE_A,
    message_id: str = MESSAGE_1,
    credential_id: str | None = CRED_A,
    matched_text: str = "needle",
    created_at: str | None = None,
) -> dict:
    return {
        "id": f"hit-{message_id[:8]}",
        "rule_id": rule_id,
        "message_id": message_id,
        "credential_id": credential_id,
        "matched_text": matched_text,
        "created_at": created_at or datetime.now(UTC).isoformat(),
    }


class _FakeResult:
    def __init__(self, data):
        self.data = data
        self.count = len(data)


class _FakeQuery:
    """Minimal PostgREST query builder backed by in-memory table lists."""

    def __init__(self, tables: dict[str, list[dict]], name: str):
        self._tables = tables
        self._name = name
        self._filters: list[tuple[str, str, object]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None
        self._insert: dict | None = None
        self._update: dict | None = None
        self._delete = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column: str, value: object):
        self._filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values):
        self._filters.append(("in", column, list(values)))
        return self

    def order(self, column: str, desc: bool = False):
        self._order = (column, desc)
        return self

    def limit(self, count: int):
        self._limit = count
        return self

    def insert(self, payload: dict):
        self._insert = dict(payload)
        return self

    def update(self, payload: dict):
        self._update = dict(payload)
        return self

    def delete(self):
        self._delete = True
        return self

    def execute(self):
        rows = self._tables.setdefault(self._name, [])
        if self._insert is not None:
            stored = dict(self._insert)
            stored.setdefault(
                "id", f"{len(rows) + 1:08d}-0000-0000-0000-000000000000"
            )
            stored.setdefault("created_at", datetime.now(UTC).isoformat())
            rows.append(stored)
            return _FakeResult([dict(stored)])
        if self._update is not None:
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self._update)
                    updated.append(dict(row))
            return _FakeResult(updated)
        if self._delete:
            kept = [row for row in rows if not self._matches(row)]
            rows[:] = kept
            return _FakeResult([])

        matched = [dict(row) for row in rows if self._matches(row)]
        if self._order is not None:
            column, desc = self._order
            matched.sort(key=lambda row: row.get(column) or "", reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        return _FakeResult(matched)

    def _matches(self, row: dict) -> bool:
        for operator, column, value in self._filters:
            actual = row.get(column)
            if operator == "eq" and str(actual) != str(value):
                return False
            if operator == "in" and str(actual) not in {str(item) for item in value}:
                return False
        return True


class _FakeDB:
    def __init__(self, rules=None, hits=None):
        self.tables = {
            "watchlist_rules": [dict(row) for row in (rules or [])],
            "watchlist_hits": [dict(row) for row in (hits or [])],
        }
        self.table_calls: list[str] = []

    def table(self, name: str):
        self.table_calls.append(name)
        return _FakeQuery(self.tables, name)


class _FakeSent:
    async def delete(self):
        return None


class _FakeMessage:
    def __init__(self, *, fail: bool = False):
        self.replies: list[dict] = []
        self._fail = fail

    async def reply_text(self, text, **kwargs):
        if self._fail:
            raise RuntimeError("telegram unavailable")
        self.replies.append({"text": text, **kwargs})
        return _FakeSent()


class _FakeUpdate:
    def __init__(self, *, fail_reply: bool = False):
        self.message = _FakeMessage(fail=fail_reply)
        self.effective_user = SimpleNamespace(id=42)
        self.effective_chat = SimpleNamespace(id=-100123, type="supergroup")


async def _run(monkeypatch, db, args, *, is_admin=True, resolver=None):
    monkeypatch.setattr(watchlist_commands, "db", db)
    monkeypatch.setattr(watchlist_commands, "is_admin", lambda _update: is_admin)
    if resolver is not None:
        monkeypatch.setattr(
            watchlist_commands, "_resolve_message_deeplink", resolver
        )
    update = _FakeUpdate()
    await watchlist_commands.watchlist_command(
        update, SimpleNamespace(args=list(args))
    )
    return update


def _reply_text(update: _FakeUpdate) -> str:
    assert len(update.message.replies) == 1
    return update.message.replies[0]["text"]


@pytest.mark.asyncio
async def test_list_formats_rules(monkeypatch):
    db = _FakeDB(
        rules=[
            _rule(RULE_A, name="Alpha", pattern="needle"),
            _rule(
                RULE_B,
                name="Beta",
                pattern="api[_-]?key" + "-" * 40,
                is_regex=True,
                credential_id=CRED_A,
                enabled=False,
                cooldown_minutes=15,
            ),
        ]
    )

    update = await _run(monkeypatch, db, [])

    reply = update.message.replies[0]
    assert reply["parse_mode"] == ParseMode.MARKDOWN
    text = reply["text"]
    assert "*Watchlist rules* (2)" in text
    assert "`aaaaaaaa`" in text
    assert "Alpha" in text
    assert "[text]" in text
    assert "[regex]" in text
    assert "GLOBAL" in text
    assert "`12345678`" in text
    assert "cd=60 on" in text
    assert "cd=15 off" in text
    # Patterns are capped around 40 characters in the listing.
    assert "-" * 50 not in text
    assert "…" in text


@pytest.mark.asyncio
async def test_list_caps_at_twenty_and_notes_more(monkeypatch):
    db = _FakeDB(
        rules=[_rule(f"{index:08d}-0000-0000-0000-000000000000") for index in range(22)]
    )

    update = await _run(monkeypatch, db, ["list"])

    text = _reply_text(update)
    assert "*Watchlist rules* (20)" in text
    assert "more rules exist" in text


@pytest.mark.asyncio
async def test_add_literal_rule(monkeypatch):
    db = _FakeDB()

    update = await _run(
        monkeypatch, db, ["add", "My", "Rule", "|", "needle"]
    )

    assert "Rule added" in _reply_text(update)
    stored = db.tables["watchlist_rules"]
    assert len(stored) == 1
    assert stored[0]["name"] == "My Rule"
    assert stored[0]["pattern"] == "needle"
    assert stored[0]["is_regex"] is False
    assert stored[0]["enabled"] is True
    assert stored[0]["credential_id"] is None
    assert stored[0]["cooldown_minutes"] == 60


@pytest.mark.asyncio
async def test_add_regex_rule(monkeypatch):
    db = _FakeDB()

    update = await _run(
        monkeypatch, db, ["add", "-r", "Keys", "|", r"api[_-]?key\s*=\s*\w+"]
    )

    assert "(regex)" in _reply_text(update)
    stored = db.tables["watchlist_rules"][0]
    assert stored["is_regex"] is True
    assert stored["pattern"] == r"api[_-]?key\s*=\s*\w+"


@pytest.mark.asyncio
async def test_add_rejects_pattern_over_two_hundred_characters(monkeypatch):
    db = _FakeDB()

    update = await _run(monkeypatch, db, ["add", "TooLong", "|", "a" * 201])

    assert "200" in _reply_text(update)
    assert db.tables["watchlist_rules"] == []


@pytest.mark.asyncio
async def test_add_rejects_invalid_regex(monkeypatch):
    db = _FakeDB()

    update = await _run(monkeypatch, db, ["add", "-r", "Broken", "|", "("])

    assert "Invalid regex" in _reply_text(update)
    assert db.tables["watchlist_rules"] == []


@pytest.mark.asyncio
async def test_add_rejects_blank_name(monkeypatch):
    db = _FakeDB()

    update = await _run(monkeypatch, db, ["add", "|", "needle"])

    assert "Rule name is required" in _reply_text(update)
    assert db.tables["watchlist_rules"] == []


@pytest.mark.asyncio
async def test_rm_deletes_by_unique_prefix(monkeypatch):
    db = _FakeDB(rules=[_rule(RULE_A), _rule(RULE_B)])

    update = await _run(monkeypatch, db, ["rm", "bbbbbbbb"])

    assert "Rule deleted" in _reply_text(update)
    assert [row["id"] for row in db.tables["watchlist_rules"]] == [RULE_A]


@pytest.mark.asyncio
async def test_rm_refuses_unknown_prefix(monkeypatch):
    db = _FakeDB(rules=[_rule(RULE_A)])

    update = await _run(monkeypatch, db, ["rm", "deadbeef"])

    assert "not found" in _reply_text(update)
    assert len(db.tables["watchlist_rules"]) == 1


@pytest.mark.asyncio
async def test_rm_refuses_ambiguous_prefix(monkeypatch):
    # Two rules share the first eight characters of their ids.
    db = _FakeDB(
        rules=[
            _rule("aaaaaaaa-1111-1111-1111-111111111111"),
            _rule("aaaaaaaa-2222-2222-2222-222222222222"),
        ]
    )

    update = await _run(monkeypatch, db, ["rm", "aaaaaaaa"])

    assert "Ambiguous prefix" in _reply_text(update)
    assert len(db.tables["watchlist_rules"]) == 2


@pytest.mark.asyncio
async def test_on_and_off_toggle_enabled(monkeypatch):
    db = _FakeDB(rules=[_rule(RULE_A, enabled=False)])

    on_update = await _run(monkeypatch, db, ["on", "aaaaaaaa"])
    assert "Rule enabled" in _reply_text(on_update)
    assert db.tables["watchlist_rules"][0]["enabled"] is True

    off_update = await _run(monkeypatch, db, ["off", "aaaaaaaa"])
    assert "Rule disabled" in _reply_text(off_update)
    assert db.tables["watchlist_rules"][0]["enabled"] is False


@pytest.mark.asyncio
async def test_cooldown_updates_rule(monkeypatch):
    db = _FakeDB(rules=[_rule(RULE_A, cooldown_minutes=60)])

    update = await _run(monkeypatch, db, ["cooldown", "aaaaaaaa", "15"])

    assert "Cooldown set to 15m" in _reply_text(update)
    assert db.tables["watchlist_rules"][0]["cooldown_minutes"] == 15


@pytest.mark.asyncio
async def test_cooldown_rejects_below_one_minute(monkeypatch):
    db = _FakeDB(rules=[_rule(RULE_A, cooldown_minutes=60)])

    update = await _run(monkeypatch, db, ["cooldown", "aaaaaaaa", "0"])

    assert "at least 1 minute" in _reply_text(update)
    assert db.tables["watchlist_rules"][0]["cooldown_minutes"] == 60


@pytest.mark.asyncio
async def test_cooldown_rejects_non_numeric(monkeypatch):
    db = _FakeDB(rules=[_rule(RULE_A, cooldown_minutes=60)])

    update = await _run(monkeypatch, db, ["cooldown", "aaaaaaaa", "soon"])

    assert "whole number" in _reply_text(update)
    assert db.tables["watchlist_rules"][0]["cooldown_minutes"] == 60


@pytest.mark.asyncio
async def test_hits_includes_deep_link_when_resolved(monkeypatch):
    db = _FakeDB(
        rules=[_rule(RULE_A, name="AWS keys")],
        hits=[_hit(matched_text="AKIA" + "x" * 120)],
    )

    async def resolver(*, message_id, credential_id):
        assert message_id == MESSAGE_1
        assert credential_id == CRED_A
        return "https://t.me/c/123456/7/99"

    update = await _run(monkeypatch, db, ["hits"], resolver=resolver)

    text = _reply_text(update)
    assert "*Recent watchlist hits* (1)" in text
    assert "AWS keys" in text
    assert "`12345678`" in text
    assert "https://t.me/c/123456/7/99" in text
    assert "x" * 90 not in text
    assert "…" in text


@pytest.mark.asyncio
async def test_hits_omits_deep_link_when_resolver_returns_none(monkeypatch):
    db = _FakeDB(
        rules=[_rule(RULE_A, name="AWS keys")],
        hits=[_hit()],
    )

    async def resolver(*, message_id, credential_id):
        return None

    update = await _run(monkeypatch, db, ["hits"], resolver=resolver)

    text = _reply_text(update)
    assert "AWS keys" in text
    assert "t.me/c/" not in text


@pytest.mark.asyncio
async def test_hits_defaults_to_five_and_caps_at_twenty(monkeypatch):
    hits = [
        _hit(message_id=f"{index:08x}-0000-0000-0000-000000000000")
        for index in range(25)
    ]
    db = _FakeDB(rules=[_rule(RULE_A)], hits=hits)

    async def resolver(*, message_id, credential_id):
        return None

    default_update = await _run(monkeypatch, db, ["hits"], resolver=resolver)
    default_text = _reply_text(default_update)
    assert default_text.count("• ") == 5

    capped_update = await _run(monkeypatch, db, ["hits", "999"], resolver=resolver)
    capped_text = _reply_text(capped_update)
    assert capped_text.count("• ") == 20


@pytest.mark.asyncio
async def test_hits_rejects_non_numeric_limit(monkeypatch):
    db = _FakeDB(rules=[_rule(RULE_A)], hits=[_hit()])

    update = await _run(monkeypatch, db, ["hits", "many"])

    assert "Usage: /watchlist hits" in _reply_text(update)


@pytest.mark.asyncio
async def test_non_admin_gets_no_output_and_no_db_access(monkeypatch):
    db = _FakeDB(rules=[_rule(RULE_A)])

    update = await _run(monkeypatch, db, ["rm", "aaaaaaaa"], is_admin=False)

    assert update.message.replies == []
    assert db.table_calls == []
    assert len(db.tables["watchlist_rules"]) == 1


@pytest.mark.asyncio
async def test_database_failure_replies_short_error_and_does_not_raise(monkeypatch):
    class _ExplodingDB:
        def table(self, _name):
            raise RuntimeError("db down")

    update = await _run(monkeypatch, _ExplodingDB(), ["list"])

    assert _reply_text(update) == "Watchlist command failed. Try again."


@pytest.mark.asyncio
async def test_unknown_subcommand_replies_usage(monkeypatch):
    db = _FakeDB()

    update = await _run(monkeypatch, db, ["bogus"])

    assert "Usage: /watchlist" in _reply_text(update)


@pytest.mark.asyncio
async def test_reply_failure_does_not_raise(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(watchlist_commands, "db", db)
    monkeypatch.setattr(watchlist_commands, "is_admin", lambda _update: True)

    update = _FakeUpdate(fail_reply=True)
    await watchlist_commands.watchlist_command(update, SimpleNamespace(args=["list"]))
