
from app.services._scraper.results import (
    ScrapeReason,
    ScrapeResult,
    ScrapeResultClassifier,
    StrategyAttempt,
)


class _FloodWaitError(Exception):
    seconds = 42


def test_classifier_maps_telegram_restrictions():
    classifier = ScrapeResultClassifier()

    attempt = classifier.classify_exception(
        Exception("The API access for bot users is restricted"),
        strategy="telethon_history",
    )

    assert attempt.reason == ScrapeReason.BOT_HISTORY_RESTRICTED
    assert attempt.retryable is False


def test_classifier_maps_webhook_too_many_bots_timeout_and_flood_wait():
    classifier = ScrapeResultClassifier()

    assert classifier.classify_exception(
        Exception("Conflict: webhook is active"),
        strategy="bot_api",
    ).reason == ScrapeReason.WEBHOOK_CONFLICT
    assert classifier.classify_exception(
        Exception("Too many bots in this chat"),
        strategy="invite",
    ).reason == ScrapeReason.TOO_MANY_BOTS
    assert classifier.classify_exception(
        TimeoutError(),
        strategy="telethon",
    ).reason == ScrapeReason.TIMEOUT
    flood = classifier.classify_exception(_FloodWaitError("Flood wait"), strategy="telethon")
    assert flood.reason == ScrapeReason.FLOOD_WAIT
    assert flood.retryable is True
    assert flood.evidence["seconds"] == 42


def test_classifier_maps_forbidden_bad_request_and_network():
    classifier = ScrapeResultClassifier()

    forbidden = classifier.classify_exception(Exception("Forbidden: bot was kicked"), strategy="bot_api")
    bad_request = classifier.classify_exception(Exception("Bad Request: chat not found"), strategy="bot_api")
    network = classifier.classify_exception(Exception("RemoteProtocolError disconnected"), strategy="bot_api")

    assert forbidden.reason == ScrapeReason.FORBIDDEN
    assert forbidden.retryable is False
    assert bad_request.reason == ScrapeReason.BAD_REQUEST
    assert bad_request.retryable is False
    assert network.reason == ScrapeReason.NETWORK_DISCONNECT
    assert network.retryable is True


def test_result_classifies_true_empty_history_after_successful_attempt():
    classifier = ScrapeResultClassifier()

    result = classifier.result_from_attempts(
        [],
        [StrategyAttempt(name="bot_api_updates", success=True, reason=ScrapeReason.NO_NEW_MESSAGES)],
    )

    assert result.reason == ScrapeReason.NO_NEW_MESSAGES
    assert result.retryable is False
    assert result.messages == []
    assert result == []


def test_result_classifies_no_accessible_updates_after_failed_attempts():
    classifier = ScrapeResultClassifier()

    result = classifier.result_from_attempts(
        [],
        [StrategyAttempt(name="bot_api_updates", success=False)],
    )

    assert result.reason == ScrapeReason.NO_ACCESSIBLE_UPDATES
    assert result.retryable is False


def test_result_prioritizes_terminal_empty_reason():
    classifier = ScrapeResultClassifier()

    result = classifier.result_from_attempts(
        [],
        [
            StrategyAttempt(name="telethon_history", success=True, reason=ScrapeReason.NO_NEW_MESSAGES),
            StrategyAttempt(name="bot_api_updates", reason=ScrapeReason.WEBHOOK_CONFLICT),
        ],
    )

    assert result.reason == ScrapeReason.WEBHOOK_CONFLICT
    assert result.next_action == "inspect_webhook_owner_or_enable_delete_policy"


def test_scrape_result_is_list_compatible_for_legacy_callers():
    result = ScrapeResult(
        messages=[{"telegram_msg_id": 1}, {"telegram_msg_id": 2}],
        reason=ScrapeReason.SUCCESS,
    )

    assert len(result) == 2
    assert result[0]["telegram_msg_id"] == 1
    assert [m["telegram_msg_id"] for m in result] == [1, 2]
    assert result == [{"telegram_msg_id": 1}, {"telegram_msg_id": 2}]


def test_scrape_result_metadata_has_reason_evidence_and_next_action():
    result = ScrapeResult(
        messages=[],
        reason=ScrapeReason.FLOOD_WAIT,
        retryable=True,
        evidence={"seconds": 30},
        strategy_attempts=[
            StrategyAttempt(name="telethon_history", reason=ScrapeReason.FLOOD_WAIT, retryable=True)
        ],
    )

    metadata = result.to_metadata()

    assert metadata["last_scrape_reason"] == "flood_wait"
    assert metadata["last_scrape_retryable"] is True
    assert metadata["last_scrape_evidence"] == {"seconds": 30}
    assert metadata["last_scrape_next_action"] == "retry_after_flood_wait"
    assert metadata["last_scrape_strategy_attempts"][0]["reason"] == "flood_wait"
