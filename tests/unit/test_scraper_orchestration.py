from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from app.services._scraper.results import ScrapeReason, StrategyAttempt
from app.services._scraper.strategies import StrategyReadOutcome, WebhookDecision
from app.services.scraper_srv import ScraperService


@dataclass
class _Reader:
    outcome: StrategyReadOutcome
    calls: int = 0

    async def read(self, *_args, **_kwargs):
        self.calls += 1
        return self.outcome


class _Preflight:
    async def ensure_bot_in_chat(self, *_args, **_kwargs):
        return StrategyAttempt(name="bot_preflight", success=True, reason=ScrapeReason.SUCCESS)


@pytest.mark.asyncio
async def test_scrape_history_short_circuits_after_rich_telethon_history(monkeypatch):
    async def no_monitor_groups():
        return set()

    service = ScraperService()
    service.bot_preflight_service = _Preflight()
    service.telethon_history_reader = _Reader(
        StrategyReadOutcome(
            messages=[
                {"telegram_msg_id": idx, "content": str(idx), "chat_id": -100}
                for idx in range(1, 12)
            ],
            attempt=StrategyAttempt(
                name="telethon_history",
                success=True,
                message_count=11,
                reason=ScrapeReason.SUCCESS,
            ),
        )
    )
    service.bot_api_update_reader = _Reader(StrategyReadOutcome())
    monkeypatch.setattr("app.services.scraper_srv._resolve_monitor_group_ids_async", no_monitor_groups)
    monkeypatch.setattr(service, "is_monitor_bot", lambda _token: False)

    result = await service.scrape_history("123:ABC", -100)

    assert result.reason == ScrapeReason.SUCCESS
    assert len(result) == 11
    assert service.bot_api_update_reader.calls == 0


@pytest.mark.asyncio
async def test_rich_telethon_short_circuit_still_checks_approved_webhook(monkeypatch):
    async def no_monitor_groups():
        return set()

    service = ScraperService()
    service.bot_preflight_service = _Preflight()
    service.telethon_history_reader = _Reader(
        StrategyReadOutcome(
            messages=[
                {"telegram_msg_id": idx, "content": str(idx), "chat_id": -100}
                for idx in range(1, 12)
            ],
            attempt=StrategyAttempt(
                name="telethon_history",
                success=True,
                message_count=11,
                reason=ScrapeReason.SUCCESS,
            ),
        )
    )
    service.bot_api_update_reader = _Reader(StrategyReadOutcome())
    webhook_decision = WebhookDecision(
        can_poll=True,
        attempt=StrategyAttempt(
            name="webhook_takeover_preflight",
            success=True,
            reason=ScrapeReason.NO_NEW_MESSAGES,
            evidence={"honeypot_repointed": True},
        ),
    )
    prepare_polling = AsyncMock(return_value=webhook_decision)
    monkeypatch.setattr(service.webhook_state_service, "prepare_polling", prepare_polling)
    monkeypatch.setattr("app.services.scraper_srv._resolve_monitor_group_ids_async", no_monitor_groups)
    monkeypatch.setattr("app.services.scraper_srv.settings.HONEYPOT_MODE", True)
    monkeypatch.setattr(
        "app.services.scraper_srv.settings.HONEYPOT_WEBHOOK_URL", "https://ours.example/receiver"
    )
    monkeypatch.setattr("app.services.scraper_srv.settings.HONEYPOT_SECRET", "secret")
    monkeypatch.setattr("app.services.scraper_srv.settings.HONEYPOT_ALLOWLIST", "AUTO")
    monkeypatch.setattr(service, "is_monitor_bot", lambda _token: False)

    result = await service.scrape_history("123:ABC", -100, credential_id="credential-1")

    assert result.reason == ScrapeReason.SUCCESS
    prepare_polling.assert_awaited_once()
    assert prepare_polling.await_args.kwargs == {
        "strategy": "webhook_takeover_preflight",
        "credential_id": "credential-1",
        "takeover_only": True,
    }
    assert service.bot_api_update_reader.calls == 0


@pytest.mark.asyncio
async def test_scrape_history_returns_webhook_conflict_when_bot_api_terminal(monkeypatch):
    async def no_monitor_groups():
        return set()

    service = ScraperService()
    service.bot_preflight_service = _Preflight()
    service.telethon_history_reader = _Reader(
        StrategyReadOutcome(
            messages=[],
            attempt=StrategyAttempt(
                name="telethon_history",
                success=True,
                reason=ScrapeReason.NO_NEW_MESSAGES,
            ),
        )
    )
    service.bot_api_update_reader = _Reader(
        StrategyReadOutcome(
            messages=[],
            attempt=StrategyAttempt(name="bot_api_updates", reason=ScrapeReason.WEBHOOK_CONFLICT),
            terminal=True,
        )
    )
    monkeypatch.setattr("app.services.scraper_srv._resolve_monitor_group_ids_async", no_monitor_groups)
    monkeypatch.setattr(service, "is_monitor_bot", lambda _token: False)

    result = await service.scrape_history("123:ABC", -100)

    assert result.reason == ScrapeReason.WEBHOOK_CONFLICT
    assert result.retryable is False
