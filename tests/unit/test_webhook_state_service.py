import pytest

from app.services._scraper.results import ScrapeReason
from app.services._scraper.strategies import BotApiUpdateReader, WebhookStateService


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []
        self.post_calls = []

    async def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self.responses.pop(0)

    async def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_webhook_present_delete_denied_is_conflict():
    client = _FakeClient([
        _Resp(payload={"ok": True, "result": {"url": "https://example.test/hook"}})
    ])
    service = WebhookStateService(allow_delete=False)

    decision = await service.prepare_polling("123:ABC", client)

    assert decision.can_poll is False
    assert decision.attempt.reason == ScrapeReason.WEBHOOK_CONFLICT
    assert client.post_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("allowlist", ["approved-credential", "AUTO"])
async def test_approved_foreign_webhook_is_repointed_and_reader_continues(
    monkeypatch, allowlist
):
    from app.services._scraper import strategies

    monkeypatch.setattr(strategies.settings, "HONEYPOT_MODE", True)
    monkeypatch.setattr(
        strategies.settings, "HONEYPOT_WEBHOOK_URL", "https://ours.example/honeypot/"
    )
    monkeypatch.setattr(strategies.settings, "HONEYPOT_SECRET", "secret-password")
    monkeypatch.setattr(strategies.settings, "HONEYPOT_ALLOWLIST", allowlist)

    client = _FakeClient([
        _Resp(payload={"ok": True, "result": {"url": "https://scammer.example/hook"}}),
        _Resp(payload={"ok": True, "result": True}),
        _Resp(status_code=409, payload={"ok": False}, text="Conflict: webhook"),
    ])

    class _FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            self.client = client

        async def __aenter__(self):
            return self.client

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(strategies.httpx, "AsyncClient", _FakeAsyncClient)
    reader = BotApiUpdateReader(
        webhook_service=WebhookStateService(allow_delete=False),
        media_formatter=lambda _payload: ("text", {}),
        is_monitor_bot=lambda _token: False,
        is_monitor_group=lambda _chat_id: False,
    )

    outcome = await reader.read("123:ABC", credential_id="approved-credential")

    assert outcome.terminal is False
    assert outcome.attempt.success is True
    assert outcome.attempt.reason == ScrapeReason.NO_NEW_MESSAGES
    assert [call[0][0].rsplit("/", 1)[-1] for call in client.get_calls] == [
        "getWebhookInfo",
        "getUpdates",
    ]
    assert len(client.post_calls) == 1
    post_url, post_kwargs = client.post_calls[0]
    assert post_url[0].endswith("/setWebhook")
    assert post_kwargs["data"] == {
        "url": "https://ours.example/honeypot/receive/approved-credential",
        "secret_token": "secret-password",
        "allowed_updates": '["message","callback_query","edited_message","channel_post","edited_channel_post","inline_query"]',
        "drop_pending_updates": "false",
    }


@pytest.mark.asyncio
async def test_unapproved_foreign_webhook_is_not_repointed(monkeypatch):
    from app.services._scraper import strategies

    monkeypatch.setattr(strategies.settings, "HONEYPOT_MODE", True)
    monkeypatch.setattr(
        strategies.settings, "HONEYPOT_WEBHOOK_URL", "https://ours.example/honeypot"
    )
    monkeypatch.setattr(strategies.settings, "HONEYPOT_SECRET", "secret-password")
    monkeypatch.setattr(strategies.settings, "HONEYPOT_ALLOWLIST", "other-credential")
    client = _FakeClient([
        _Resp(payload={"ok": True, "result": {"url": "https://scammer.example/hook"}})
    ])

    decision = await WebhookStateService(allow_delete=False).prepare_polling(
        "123:ABC", client, credential_id="unapproved-credential"
    )

    assert decision.can_poll is False
    assert decision.attempt.reason == ScrapeReason.WEBHOOK_CONFLICT
    assert client.post_calls == []


@pytest.mark.asyncio
async def test_takeover_only_repoints_approved_webhook_without_delete_fallback(monkeypatch):
    from app.services._scraper import strategies

    monkeypatch.setattr(strategies.settings, "HONEYPOT_MODE", True)
    monkeypatch.setattr(
        strategies.settings, "HONEYPOT_WEBHOOK_URL", "https://ours.example/honeypot"
    )
    monkeypatch.setattr(strategies.settings, "HONEYPOT_SECRET", "secret-password")
    monkeypatch.setattr(strategies.settings, "HONEYPOT_ALLOWLIST", "AUTO")

    client = _FakeClient([
        _Resp(payload={"ok": True, "result": {"url": "https://scammer.example/hook"}}),
        _Resp(payload={"ok": True, "result": True}),
    ])
    decision = await WebhookStateService(allow_delete=True).prepare_polling(
        "123:ABC",
        client,
        credential_id="credential-1",
        takeover_only=True,
    )

    assert decision.can_poll is True
    assert decision.attempt.evidence["honeypot_repointed"] is True
    assert len(client.post_calls) == 1
    assert client.post_calls[0][0][0].endswith("/setWebhook")


@pytest.mark.asyncio
async def test_webhook_present_delete_allowed_retries_polling():
    client = _FakeClient([
        _Resp(payload={"ok": True, "result": {"url": "https://example.test/hook"}}),
        _Resp(payload={"ok": True, "result": True}),
    ])
    service = WebhookStateService(allow_delete=True)

    decision = await service.prepare_polling("123:ABC", client)

    assert decision.can_poll is True
    assert decision.attempt.success is True
    assert len(client.post_calls) == 1


@pytest.mark.asyncio
async def test_webhook_delete_failure_is_conflict():
    client = _FakeClient([
        _Resp(payload={"ok": True, "result": {"url": "https://example.test/hook"}}),
        _Resp(status_code=500, payload={"ok": False, "description": "failed"}),
    ])
    service = WebhookStateService(allow_delete=True)

    decision = await service.prepare_polling("123:ABC", client)

    assert decision.can_poll is False
    assert decision.attempt.reason == ScrapeReason.WEBHOOK_CONFLICT


@pytest.mark.asyncio
async def test_bot_api_reader_classifies_getupdates_409(monkeypatch):
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.client = _FakeClient([
                _Resp(payload={"ok": True, "result": {}}),
                _Resp(status_code=409, payload={"ok": False}, text="Conflict: webhook"),
            ])

        async def __aenter__(self):
            return self.client

        async def __aexit__(self, *_args):
            return False

    from app.services._scraper import strategies

    monkeypatch.setattr(strategies.httpx, "AsyncClient", _FakeAsyncClient)
    reader = BotApiUpdateReader(
        webhook_service=WebhookStateService(allow_delete=False),
        media_formatter=lambda _payload: ("text", {}),
        is_monitor_bot=lambda _token: False,
        is_monitor_group=lambda _chat_id: False,
    )

    outcome = await reader.read("123:ABC")

    assert outcome.terminal is True
    assert outcome.attempt.reason == ScrapeReason.WEBHOOK_CONFLICT
