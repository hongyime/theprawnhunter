"""Watchlist alert dispatch: one alert per hit, failure-isolated."""

import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import watchlist_alerts

MESSAGE_ID = "msg-1"
CREDENTIAL_ID = "12345678-aaaa-bbbb-cccc-dddddddddddd"


def _hit(rule_id="rule-1", name="AWS keys", matched_text="AKIAEXAMPLE"):
    return {
        "rule_id": rule_id,
        "name": name,
        "matched_text": matched_text,
        "credential_id": CREDENTIAL_ID,
    }


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._rows))


class _FakeDB:
    """Minimal PostgREST-shaped fake: one fixed row set per table."""

    def __init__(self, tables):
        self.tables = tables
        self.requested = []

    def table(self, name):
        self.requested.append(name)
        return _FakeQuery(self.tables.get(name, []))


class _FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.gets = []
        self.setex_calls = []

    def get(self, key):
        self.gets.append(key)
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value


def _patch_redis(monkeypatch, fake_redis):
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(
            from_url=lambda _url, decode_responses=True: fake_redis
        ),
    )


def _patch_group_lookup(broadcaster, *, chat_id=None, error=None):
    """Give the fake broadcaster a Bot whose ``get_chat`` returns ``chat_id``."""
    get_chat = AsyncMock()
    if error is not None:
        get_chat.side_effect = error
    else:
        get_chat.return_value = SimpleNamespace(id=chat_id)
    broadcaster.bot_tokens = ["123:ABC"]
    broadcaster._get_bot_instance = lambda _token: SimpleNamespace(
        get_chat=get_chat
    )
    return get_chat


def _patch_deeplink_tables(
    monkeypatch,
    broadcaster,
    *,
    group_id=-1001234567890,
    credential_rows=None,
    message_rows=None,
    thread_id=789,
):
    """Wire config/DB/topic resolution for a successful deep link."""
    if credential_rows is None:
        credential_rows = [{"meta": {"bot_username": "@format_fm_bot"}}]
    if message_rows is None:
        message_rows = [{"telegram_msg_id": 456, "broadcast_message_id": 456}]
    monkeypatch.setattr(
        watchlist_alerts,
        "settings",
        SimpleNamespace(
            MONITOR_GROUP_ID=group_id,
            REDIS_URL="redis://localhost:6379/0",
        ),
    )
    fake_db = _FakeDB(
        {
            "discovered_credentials": credential_rows,
            "exfiltrated_messages": message_rows,
        }
    )
    monkeypatch.setattr(watchlist_alerts, "db", fake_db)
    broadcaster.find_existing_topic_id = AsyncMock(return_value=thread_id)
    return fake_db


@pytest.fixture(autouse=True)
def _no_deeplink_config(monkeypatch):
    """Keep existing tests offline: no group id means no DB/topic lookup."""
    monkeypatch.setattr(
        watchlist_alerts,
        "settings",
        SimpleNamespace(MONITOR_GROUP_ID=None),
    )


@pytest.fixture
def broadcaster(monkeypatch):
    fake = SimpleNamespace(
        send_watchlist_alert=AsyncMock(return_value=True),
        send_alert=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.workers.tasks.flow_tasks.get_broadcaster", lambda: fake
    )
    return fake


def _mock_evaluate(monkeypatch, result):
    evaluate = AsyncMock(return_value=result)
    monkeypatch.setattr(watchlist_alerts.watchlists, "evaluate", evaluate)
    return evaluate


@pytest.mark.asyncio
async def test_one_match_sends_exactly_one_alert_with_expected_fields(
    monkeypatch, broadcaster
):
    evaluate = _mock_evaluate(monkeypatch, [_hit()])

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE here",
        message_id=MESSAGE_ID,
        credential_id=CREDENTIAL_ID,
    )

    assert sent == 1
    assert broadcaster.send_watchlist_alert.await_count == 1
    broadcaster.send_alert.assert_not_awaited()
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "AWS keys" in body
    assert "12345678" in body
    assert "AKIAEXAMPLE" in body
    assert MESSAGE_ID in body
    assert "Jump:" in body
    evaluate.assert_awaited_once_with(
        "secret AKIAEXAMPLE here",
        message_id=MESSAGE_ID,
        credential_id=CREDENTIAL_ID,
    )


@pytest.mark.asyncio
async def test_matched_text_is_truncated_to_300_chars(monkeypatch, broadcaster):
    _mock_evaluate(monkeypatch, [_hit(matched_text="x" * 500)])

    sent = await watchlist_alerts.alert_on_matches(
        "anything", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "x" * 300 in body
    assert "x" * 301 not in body


@pytest.mark.asyncio
async def test_no_match_sends_nothing(monkeypatch, broadcaster):
    _mock_evaluate(monkeypatch, [])

    sent = await watchlist_alerts.alert_on_matches(
        "benign text", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
    )

    assert sent == 0
    broadcaster.send_watchlist_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_returns_no_hits_and_sends_nothing(monkeypatch, broadcaster):
    # evaluate() already drops rules on cooldown; it reports an empty list.
    _mock_evaluate(monkeypatch, [])

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
    )

    assert sent == 0
    broadcaster.send_watchlist_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcaster_failure_is_swallowed(monkeypatch, broadcaster, caplog):
    _mock_evaluate(monkeypatch, [_hit()])
    broadcaster.send_watchlist_alert.side_effect = RuntimeError("telegram down")

    with caplog.at_level(logging.WARNING, logger="app.services.watchlist_alerts"):
        sent = await watchlist_alerts.alert_on_matches(
            "secret AKIAEXAMPLE", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
        )

    assert sent == 0
    assert "alert dispatch failed" in caplog.text
    # No message content in the log.
    assert "AKIAEXAMPLE" not in caplog.text


@pytest.mark.asyncio
async def test_evaluate_failure_is_swallowed(monkeypatch, broadcaster, caplog):
    evaluate = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(watchlist_alerts.watchlists, "evaluate", evaluate)

    with caplog.at_level(logging.WARNING, logger="app.services.watchlist_alerts"):
        sent = await watchlist_alerts.alert_on_matches(
            "secret AKIAEXAMPLE", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
        )

    assert sent == 0
    broadcaster.send_watchlist_alert.assert_not_awaited()
    assert "evaluation failed" in caplog.text


@pytest.mark.asyncio
async def test_two_matching_rules_send_two_alerts(monkeypatch, broadcaster):
    _mock_evaluate(
        monkeypatch,
        [
            _hit(rule_id="rule-1", name="Rule one", matched_text="first"),
            _hit(rule_id="rule-2", name="Rule two", matched_text="second"),
        ],
    )

    sent = await watchlist_alerts.alert_on_matches(
        "first and second", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
    )

    assert sent == 2
    assert broadcaster.send_watchlist_alert.await_count == 2
    bodies = [call.args[0] for call in broadcaster.send_watchlist_alert.await_args_list]
    assert any("Rule one" in body and "first" in body for body in bodies)
    assert any("Rule two" in body and "second" in body for body in bodies)


@pytest.mark.asyncio
async def test_duplicate_message_evaluates_once_per_rule(monkeypatch, broadcaster):
    # watchlists.evaluate dedupes via the (rule_id, message_id) hit table; the
    # second evaluation of the same message emits no hits.
    evaluate = AsyncMock(side_effect=[[_hit()], []])
    monkeypatch.setattr(watchlist_alerts.watchlists, "evaluate", evaluate)

    first = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
    )
    second = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
    )

    assert (first, second) == (1, 0)
    assert broadcaster.send_watchlist_alert.await_count == 1


@pytest.mark.asyncio
async def test_scrape_call_site_survives_helper_exception(monkeypatch, caplog):
    from app.workers.tasks.flows import scrape

    monkeypatch.setattr(
        watchlist_alerts,
        "alert_on_matches",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    with caplog.at_level(logging.WARNING, logger="flow.tasks"):
        await scrape._alert_stored_rows(
            [{"id": "stored-row-1", "content": "hello", "credential_id": CREDENTIAL_ID}]
        )

    assert "watchlist alert skipped" in caplog.text


@pytest.mark.asyncio
async def test_broadcast_call_site_survives_helper_exception(monkeypatch, caplog):
    from app.workers.tasks.flows import broadcast

    monkeypatch.setattr(
        watchlist_alerts,
        "alert_on_matches",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    with caplog.at_level(logging.WARNING, logger="flow.tasks"):
        result = await broadcast._alert_watchlist(
            {"id": "stored-row-2", "content": "hello", "credential_id": CREDENTIAL_ID}
        )

    assert result is None
    assert "watchlist alert skipped" in caplog.text


def test_build_message_deeplink_normalizes_supergroup_ids():
    expected = "https://t.me/c/1234567890/789/456"
    assert watchlist_alerts.build_message_deeplink("-1001234567890", 789, 456) == expected
    assert watchlist_alerts.build_message_deeplink(-1001234567890, 789, 456) == expected
    assert watchlist_alerts.build_message_deeplink("-1234567890", 789, 456) == expected
    # Missing or non-numeric components omit the link.
    assert watchlist_alerts.build_message_deeplink(None, 789, 456) is None
    assert (
        watchlist_alerts.build_message_deeplink("-1001234567890", 789, None)
        == "https://t.me/c/1234567890/789"
    )
    assert watchlist_alerts.build_message_deeplink("-1001234567890", "topic", 456) is None


@pytest.mark.asyncio
async def test_alert_appends_exact_deeplink_to_watchlist_body(monkeypatch, broadcaster):
    _mock_evaluate(monkeypatch, [_hit()])
    fake_db = _patch_deeplink_tables(monkeypatch, broadcaster)

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id="row-1", credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert body.endswith(
        "📎 Open in Telegram: https://t.me/c/1234567890/789/456"
    )
    broadcaster.find_existing_topic_id.assert_awaited_once_with(
        -1001234567890, "@format_fm_bot"
    )
    assert fake_db.requested == ["discovered_credentials", "exfiltrated_messages"]


@pytest.mark.asyncio
async def test_string_group_id_produces_correct_internal_id(monkeypatch, broadcaster):
    _mock_evaluate(monkeypatch, [_hit()])
    _patch_deeplink_tables(
        monkeypatch, broadcaster, group_id="-1001234567890"
    )

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id="row-1", credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "https://t.me/c/1234567890/789/456" in body


@pytest.mark.asyncio
async def test_missing_broadcast_id_links_to_the_topic(
    monkeypatch, broadcaster
):
    _mock_evaluate(monkeypatch, [_hit()])
    _patch_deeplink_tables(
        monkeypatch,
        broadcaster,
        message_rows=[{"telegram_msg_id": 456, "broadcast_message_id": None}],
    )

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id="row-1", credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "AKIAEXAMPLE" in body
    assert "https://t.me/c/1234567890/789" in body
    assert "https://t.me/c/1234567890/789/" not in body


@pytest.mark.asyncio
async def test_unknown_topic_omits_link_but_sends_alert(
    monkeypatch, broadcaster
):
    _mock_evaluate(monkeypatch, [_hit()])
    _patch_deeplink_tables(monkeypatch, broadcaster)
    broadcaster.find_existing_topic_id = AsyncMock(return_value=None)

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id="row-1", credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "AKIAEXAMPLE" in body
    assert "Open in Telegram" not in body


@pytest.mark.asyncio
async def test_missing_monitor_group_id_omits_link_but_sends_alert(
    monkeypatch, broadcaster
):
    _mock_evaluate(monkeypatch, [_hit()])
    # The autouse fixture already leaves MONITOR_GROUP_ID unset; no DB is wired.
    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id="row-1", credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "AKIAEXAMPLE" in body
    assert "Open in Telegram" not in body


def test_internal_group_id_rejects_non_numeric_junk():
    for junk in ("@theprawnhunter", "abc", "", "   ", "-", None, "123abc", "-100"):
        assert watchlist_alerts._internal_group_id(junk) is None
    assert watchlist_alerts._internal_group_id("-1001234567890") == "1234567890"
    assert watchlist_alerts._internal_group_id("-1234567890") == "1234567890"
    assert watchlist_alerts._internal_group_id(1234567890) == "1234567890"


@pytest.mark.asyncio
async def test_username_group_id_resolves_numeric_id_for_deeplink(
    monkeypatch, broadcaster
):
    _mock_evaluate(monkeypatch, [_hit()])
    fake_redis = _FakeRedis()
    _patch_redis(monkeypatch, fake_redis)
    _patch_deeplink_tables(
        monkeypatch, broadcaster, group_id="@theprawnhunter"
    )
    get_chat = _patch_group_lookup(broadcaster, chat_id=-1009876543210)

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id="row-1", credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert body.endswith(
        "📎 Open in Telegram: https://t.me/c/9876543210/789/456"
    )
    get_chat.assert_awaited_once_with("@theprawnhunter")
    broadcaster.find_existing_topic_id.assert_awaited_once_with(
        "@theprawnhunter", "@format_fm_bot"
    )
    assert fake_redis.setex_calls == [
        ("group:numeric_id:@theprawnhunter", 604800, "-1009876543210")
    ]


@pytest.mark.asyncio
async def test_resolved_numeric_group_id_is_cached_across_calls(
    monkeypatch, broadcaster
):
    fake_redis = _FakeRedis()
    _patch_redis(monkeypatch, fake_redis)
    _patch_deeplink_tables(
        monkeypatch, broadcaster, group_id="@theprawnhunter"
    )
    get_chat = _patch_group_lookup(broadcaster, chat_id=-1009876543210)

    first = await watchlist_alerts._resolve_message_deeplink(
        message_id="row-1", credential_id=CREDENTIAL_ID
    )
    second = await watchlist_alerts._resolve_message_deeplink(
        message_id="row-1", credential_id=CREDENTIAL_ID
    )

    expected = "https://t.me/c/9876543210/789/456"
    assert (first, second) == (expected, expected)
    get_chat.assert_awaited_once_with("@theprawnhunter")
    assert fake_redis.gets.count("group:numeric_id:@theprawnhunter") == 2
    assert fake_redis.setex_calls == [
        ("group:numeric_id:@theprawnhunter", 604800, "-1009876543210")
    ]


@pytest.mark.asyncio
async def test_get_chat_failure_omits_link_but_sends_alert(
    monkeypatch, broadcaster, caplog
):
    _mock_evaluate(monkeypatch, [_hit()])
    fake_redis = _FakeRedis()
    _patch_redis(monkeypatch, fake_redis)
    _patch_deeplink_tables(
        monkeypatch, broadcaster, group_id="@theprawnhunter"
    )
    _patch_group_lookup(
        broadcaster, error=RuntimeError("bot api unavailable")
    )

    with caplog.at_level(
        logging.WARNING, logger="app.services.watchlist_alerts"
    ):
        sent = await watchlist_alerts.alert_on_matches(
            "secret AKIAEXAMPLE",
            message_id="row-1",
            credential_id=CREDENTIAL_ID,
        )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "AKIAEXAMPLE" in body
    assert "Open in Telegram" not in body
    assert "could not resolve numeric id" in caplog.text
    assert fake_redis.setex_calls == []


@pytest.mark.asyncio
async def test_numeric_group_id_skips_get_chat_entirely(
    monkeypatch, broadcaster
):
    _mock_evaluate(monkeypatch, [_hit()])
    fake_redis = _FakeRedis()
    _patch_redis(monkeypatch, fake_redis)
    _patch_deeplink_tables(monkeypatch, broadcaster)
    get_chat = _patch_group_lookup(broadcaster, chat_id=-1009999999999)

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id="row-1", credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "https://t.me/c/1234567890/789/456" in body
    get_chat.assert_not_awaited()
    assert fake_redis.gets == []
    assert fake_redis.setex_calls == []


@pytest.mark.asyncio
async def test_stored_topic_id_wins_over_title_lookup(monkeypatch, broadcaster):
    _mock_evaluate(monkeypatch, [_hit()])
    _patch_deeplink_tables(
        monkeypatch,
        broadcaster,
        credential_rows=[{"meta": {"bot_username": "@xiaoai33bot", "topic_id": "964633"}}],
        message_rows=[{"telegram_msg_id": 879951, "broadcast_message_id": 1133781}],
    )

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id="row-1", credential_id=CREDENTIAL_ID
    )

    assert sent == 1
    body = broadcaster.send_watchlist_alert.await_args.args[0]
    assert "https://t.me/c/1234567890/964633/1133781" in body
    broadcaster.find_existing_topic_id.assert_not_awaited()
