from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.workers.tasks.honeypot_redirect_strategies import (
    HoneypotRedirectStrategies,
    TelegramDeliveryResult,
)


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict:
        return self._body


class FakeClient:
    response = FakeResponse({"ok": True, "result": True})

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, *_args, **_kwargs) -> FakeResponse:
        return self.response


@pytest.mark.asyncio
async def test_send_message_helpers_preserve_message_id() -> None:
    FakeClient.response = FakeResponse({"ok": True, "result": {"message_id": 4202}})

    with patch("httpx.AsyncClient", FakeClient):
        reminder = await HoneypotRedirectStrategies.send_multi_touch_message(
            "token",
            123,
            2,
            "https://t.me/example?start=migrate",
            "example",
        )
        proactive = await HoneypotRedirectStrategies.send_proactive_inline_request(
            "token",
            123,
            "example",
        )

    assert reminder == TelegramDeliveryResult(True, "sendMessage", 4202, None)
    assert proactive == TelegramDeliveryResult(True, "sendMessage", 4202, None)


@pytest.mark.asyncio
async def test_callback_and_inline_acknowledgements_have_no_message_id() -> None:
    FakeClient.response = FakeResponse({"ok": True, "result": True})

    with patch("httpx.AsyncClient", FakeClient):
        callback = await HoneypotRedirectStrategies.send_callback_hijack(
            "token",
            "callback-id",
            "https://t.me/example?start=migrate",
            "example",
        )
        inline = await HoneypotRedirectStrategies.send_inline_hijack(
            "token",
            "inline-id",
            "query",
            "https://t.me/example?start=migrate",
            "example",
        )

    assert callback == TelegramDeliveryResult(
        True,
        "answerCallbackQuery",
        None,
        None,
    )
    assert inline == TelegramDeliveryResult(True, "answerInlineQuery", None, None)


@pytest.mark.asyncio
async def test_malformed_send_message_response_is_not_delivery_evidence() -> None:
    FakeClient.response = FakeResponse({"ok": True, "result": {}})

    with patch("httpx.AsyncClient", FakeClient):
        delivery = await HoneypotRedirectStrategies.send_multi_touch_message(
            "token",
            123,
            2,
            "https://t.me/example?start=migrate",
            "example",
        )

    assert delivery == TelegramDeliveryResult(
        False,
        "sendMessage",
        None,
        "Telegram sendMessage response missing message_id",
    )


@pytest.mark.asyncio
async def test_redirect_record_persists_ack_method_with_null_message_id(monkeypatch) -> None:
    updates: list[dict] = []

    class FakeQuery:
        def update(self, payload: dict):
            updates.append(payload)
            return self

        def eq(self, *_args):
            return self

    class FakeDb:
        def table(self, _name: str) -> FakeQuery:
            return FakeQuery()

    async def fake_async_execute(_query: FakeQuery) -> SimpleNamespace:
        return SimpleNamespace(data=[{"id": "update-id"}])

    import app.workers.tasks.honeypot_redirect_strategies as strategies

    monkeypatch.setattr(strategies, "db", FakeDb())
    monkeypatch.setattr(strategies, "async_execute", fake_async_execute)
    delivery = TelegramDeliveryResult(True, "answerCallbackQuery", None, None)

    updated = await HoneypotRedirectStrategies.update_redirect_record(
        "update-id",
        123,
        "example",
        delivery,
        claim_token="claim-token",
    )

    assert updated is True
    assert updates[0]["redirect_method"] == "answerCallbackQuery"
    assert updates[0]["redirect_1_message_id"] is None
