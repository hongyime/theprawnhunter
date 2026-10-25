"""Watchlist alert dispatch: one alert per hit, failure-isolated."""

import logging
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


@pytest.fixture
def broadcaster(monkeypatch):
    fake = SimpleNamespace(send_alert=AsyncMock(return_value=True))
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
    assert broadcaster.send_alert.await_count == 1
    body = broadcaster.send_alert.await_args.args[0]
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
    body = broadcaster.send_alert.await_args.args[0]
    assert "x" * 300 in body
    assert "x" * 301 not in body


@pytest.mark.asyncio
async def test_no_match_sends_nothing(monkeypatch, broadcaster):
    _mock_evaluate(monkeypatch, [])

    sent = await watchlist_alerts.alert_on_matches(
        "benign text", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
    )

    assert sent == 0
    broadcaster.send_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_returns_no_hits_and_sends_nothing(monkeypatch, broadcaster):
    # evaluate() already drops rules on cooldown; it reports an empty list.
    _mock_evaluate(monkeypatch, [])

    sent = await watchlist_alerts.alert_on_matches(
        "secret AKIAEXAMPLE", message_id=MESSAGE_ID, credential_id=CREDENTIAL_ID
    )

    assert sent == 0
    broadcaster.send_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcaster_failure_is_swallowed(monkeypatch, broadcaster, caplog):
    _mock_evaluate(monkeypatch, [_hit()])
    broadcaster.send_alert.side_effect = RuntimeError("telegram down")

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
    broadcaster.send_alert.assert_not_awaited()
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
    assert broadcaster.send_alert.await_count == 2
    bodies = [call.args[0] for call in broadcaster.send_alert.await_args_list]
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
    assert broadcaster.send_alert.await_count == 1


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
