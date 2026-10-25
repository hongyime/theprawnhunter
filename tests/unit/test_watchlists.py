"""Unit tests for watchlist evaluation (no live database or Redis)."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.services import watchlists

CRED_A = "11111111-1111-1111-1111-111111111111"
CRED_B = "22222222-2222-2222-2222-222222222222"
MSG_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
MSG_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
RULE_1 = "cccccccc-cccc-cccc-cccc-cccccccccccc"
RULE_2 = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _rule(
    identifier: str,
    name: str,
    pattern: str,
    *,
    is_regex: bool = False,
    credential_id: str | None = None,
    cooldown_minutes: int = 60,
) -> dict:
    return {
        "id": identifier,
        "name": name,
        "pattern": pattern,
        "is_regex": is_regex,
        "credential_id": credential_id,
        "enabled": True,
        "cooldown_minutes": cooldown_minutes,
    }


def _hit(
    rule_id: str,
    message_id: str,
    *,
    credential_id: str | None = CRED_A,
    created_at: datetime | None = None,
) -> dict:
    return {
        "id": f"seed-{rule_id[:8]}-{message_id[:8]}",
        "rule_id": rule_id,
        "message_id": message_id,
        "credential_id": credential_id,
        "matched_text": "seed",
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
    }


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows, upsert_handler):
        self._rows = rows
        self._upsert_handler = upsert_handler
        self._filters: list[tuple[str, str, object]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None
        self._upsert_payload: dict | None = None
        self._ignore_duplicates = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column: str, value: object):
        self._filters.append(("eq", column, value))
        return self

    def gte(self, column: str, value: object):
        self._filters.append(("gte", column, value))
        return self

    def is_(self, column: str, value: object):
        self._filters.append(("is", column, value))
        return self

    def order(self, column: str, desc: bool = False):
        self._order = (column, desc)
        return self

    def limit(self, count: int):
        self._limit = count
        return self

    def upsert(self, payload: dict, on_conflict: str = "", ignore_duplicates: bool = False):
        assert on_conflict == "rule_id,message_id"
        self._upsert_payload = payload
        self._ignore_duplicates = ignore_duplicates
        return self

    def execute(self):
        if self._upsert_payload is not None:
            return _FakeResult(
                self._upsert_handler(self._upsert_payload, self._ignore_duplicates)
            )
        rows = [dict(row) for row in self._rows if self._matches(row)]
        if self._order is not None:
            column, desc = self._order
            rows.sort(key=lambda row: row.get(column) or "", reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResult(rows)

    def _matches(self, row: dict) -> bool:
        for operator, column, value in self._filters:
            actual = row.get(column)
            if operator == "eq" and str(actual) != str(value):
                return False
            if operator == "gte" and str(actual or "") < str(value):
                return False
            if operator == "is" and value == "null" and actual is not None:
                return False
        return True


class _FakeWatchlistClient:
    def __init__(self, rules: list[dict], hits: list[dict] | None = None):
        self.rules = list(rules)
        self.hits = list(hits or [])
        self.insert_attempts: list[dict] = []

    def table(self, name: str):
        if name == "watchlist_rules":
            return _FakeQuery(self.rules, self._unexpected_upsert)
        if name == "watchlist_hits":
            return _FakeQuery(self.hits, self._upsert_hit)
        raise AssertionError(f"unexpected table {name}")

    @staticmethod
    def _unexpected_upsert(_payload: dict, _ignore: bool):
        raise AssertionError("watchlist_rules is read-only for the evaluator")

    def _upsert_hit(self, payload: dict, ignore_duplicates: bool):
        self.insert_attempts.append(payload)
        key = (payload["rule_id"], payload["message_id"])
        for row in self.hits:
            if (row["rule_id"], row["message_id"]) == key:
                # ON CONFLICT DO NOTHING returns no representation rows.
                return [] if ignore_duplicates else [dict(row)]
        stored = dict(payload)
        stored["id"] = f"hit-{len(self.hits) + 1}"
        stored["created_at"] = datetime.now(UTC).isoformat()
        self.hits.append(stored)
        return [dict(stored)]


@pytest.fixture(autouse=True)
def _clear_rule_cache():
    watchlists.clear_cache()
    yield
    watchlists.clear_cache()


@pytest.mark.asyncio
async def test_literal_rule_matches_case_insensitively():
    rule = _rule(RULE_1, "literal", "SecretProject")
    client = _FakeWatchlistClient([rule])

    hits = await watchlists.evaluate(
        "we mention secretproject daily",
        message_id=MSG_1,
        credential_id=CRED_A,
        client=client,
    )

    assert len(hits) == 1
    assert hits[0] == {
        "rule_id": RULE_1,
        "name": "literal",
        "matched_text": "secretproject",
        "credential_id": CRED_A,
    }
    assert len(client.hits) == 1
    assert client.hits[0]["matched_text"] == "secretproject"


@pytest.mark.asyncio
async def test_literal_rule_does_not_interpret_regex_characters():
    rule = _rule(RULE_1, "literal-dot", "a.b")
    client = _FakeWatchlistClient([rule])

    no_match = await watchlists.evaluate(
        "axb", message_id=MSG_1, credential_id=CRED_A, client=client
    )
    match = await watchlists.evaluate(
        "a.b", message_id=MSG_2, credential_id=CRED_A, client=client
    )

    assert no_match == []
    assert len(match) == 1
    assert match[0]["matched_text"] == "a.b"


@pytest.mark.asyncio
async def test_regex_rule_matches():
    rule = _rule(
        RULE_1,
        "api-key",
        r"api[_-]?key\s*=\s*\w+",
        is_regex=True,
    )
    client = _FakeWatchlistClient([rule])

    hits = await watchlists.evaluate(
        "leaked API_KEY = abc123 here",
        message_id=MSG_1,
        credential_id=CRED_A,
        client=client,
    )

    assert len(hits) == 1
    assert hits[0]["matched_text"] == "API_KEY = abc123"


@pytest.mark.asyncio
async def test_oversized_regex_pattern_is_skipped(caplog):
    rule = _rule(RULE_1, "too-long", "a" * 201, is_regex=True)
    client = _FakeWatchlistClient([rule])

    with caplog.at_level(logging.WARNING, logger="app.services.watchlists"):
        hits = await watchlists.evaluate(
            "a" * 500, message_id=MSG_1, credential_id=CRED_A, client=client
        )

    assert hits == []
    assert client.insert_attempts == []
    assert "exceeds 200" in caplog.text


@pytest.mark.asyncio
async def test_catastrophic_regex_times_out_and_returns_no_match():
    rule = _rule(RULE_1, "evil", r"(a+)+$", is_regex=True)
    client = _FakeWatchlistClient([rule])

    started = time.monotonic()
    hits = await watchlists.evaluate(
        "a" * 40 + "b", message_id=MSG_1, credential_id=CRED_A, client=client
    )
    elapsed = time.monotonic() - started

    assert hits == []
    assert client.insert_attempts == []
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_invalid_regex_does_not_abort_other_rules():
    broken = _rule(RULE_1, "broken", "(", is_regex=True)
    good = _rule(RULE_2, "good", "needle")
    client = _FakeWatchlistClient([broken, good])

    hits = await watchlists.evaluate(
        "a needle in the haystack",
        message_id=MSG_1,
        credential_id=CRED_A,
        client=client,
    )

    assert [hit["name"] for hit in hits] == ["good"]


@pytest.mark.asyncio
async def test_duplicate_rule_message_pair_is_not_reinserted():
    rule = _rule(RULE_1, "dup", "badactor", cooldown_minutes=1)
    # Cooldown has expired, so the unique constraint is what suppresses this.
    client = _FakeWatchlistClient(
        [rule],
        hits=[
            _hit(
                RULE_1,
                MSG_1,
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        ],
    )

    hits = await watchlists.evaluate(
        "badactor returns", message_id=MSG_1, credential_id=CRED_A, client=client
    )

    assert hits == []
    assert len(client.insert_attempts) == 1
    assert len(client.hits) == 1


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_hit_for_same_credential():
    rule = _rule(RULE_1, "cooldown", "badactor", cooldown_minutes=60)
    client = _FakeWatchlistClient([rule])

    first = await watchlists.evaluate(
        "badactor one", message_id=MSG_1, credential_id=CRED_A, client=client
    )
    second = await watchlists.evaluate(
        "badactor two", message_id=MSG_2, credential_id=CRED_A, client=client
    )

    assert len(first) == 1
    assert second == []
    assert len(client.insert_attempts) == 1
    assert len(client.hits) == 1


@pytest.mark.asyncio
async def test_expired_cooldown_allows_a_new_hit():
    rule = _rule(RULE_1, "cooldown", "badactor", cooldown_minutes=60)
    client = _FakeWatchlistClient(
        [rule],
        hits=[
            _hit(
                RULE_1,
                MSG_1,
                created_at=datetime.now(UTC) - timedelta(minutes=120),
            )
        ],
    )

    hits = await watchlists.evaluate(
        "badactor again", message_id=MSG_2, credential_id=CRED_A, client=client
    )

    assert len(hits) == 1
    assert len(client.hits) == 2


@pytest.mark.asyncio
async def test_non_matching_and_empty_content_return_no_hits():
    rule = _rule(RULE_1, "literal", "badactor")
    client = _FakeWatchlistClient([rule])

    assert (
        await watchlists.evaluate(
            "nothing to see", message_id=MSG_1, credential_id=CRED_A, client=client
        )
        == []
    )
    assert (
        await watchlists.evaluate(
            "   ", message_id=MSG_2, credential_id=CRED_A, client=client
        )
        == []
    )
    assert client.insert_attempts == []


@pytest.mark.asyncio
async def test_rule_scoped_to_another_credential_is_ignored():
    rule = _rule(RULE_1, "scoped", "badactor", credential_id=CRED_B)
    client = _FakeWatchlistClient([rule])

    hits = await watchlists.evaluate(
        "badactor", message_id=MSG_1, credential_id=CRED_A, client=client
    )

    assert hits == []
    assert client.insert_attempts == []
