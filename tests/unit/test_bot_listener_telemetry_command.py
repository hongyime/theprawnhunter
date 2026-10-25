import pytest
from telegram.constants import ParseMode

from app.services import bot_listener


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})


class _FakeUpdate:
    def __init__(self):
        self.message = _FakeMessage()


@pytest.mark.asyncio
async def test_telemetry_command_sends_markdown_v2_summary(monkeypatch):
    update = _FakeUpdate()

    async def fake_summary():
        return {
            "counts": {
                "network_domain": 12,
                "canonical_url": 7,
                "wallet_address": 3,
            },
            "recent_wallets": [
                {"indicator_value": "0x1111111111111111111111111111111111111111"},
            ],
            "recent_domains": [
                {"indicator_value": "api.remote.net"},
            ],
            "gateway_credentials": [
                {
                    "meta": {
                        "gateway_telemetry": {
                            "configured_webhook_url": "https://hook.remote.net/telegram",
                        }
                    }
                }
            ],
        }

    monkeypatch.setattr(bot_listener, "is_admin", lambda _update: True)
    monkeypatch.setattr(bot_listener, "_fetch_telemetry_summary", fake_summary)

    await bot_listener.telemetry_command(update, None)

    assert len(update.message.replies) == 1
    reply = update.message.replies[0]
    assert reply["parse_mode"] == ParseMode.MARKDOWN_V2
    assert "Telemetry Analytics" in reply["text"]
    assert "`12`" in reply["text"]
    assert "0x1111111111111111111111111111111111111111" in reply["text"]
    assert reply["disable_web_page_preview"] is True


@pytest.mark.asyncio
async def test_telemetry_command_restricts_non_admin(monkeypatch):
    update = _FakeUpdate()
    monkeypatch.setattr(bot_listener, "is_admin", lambda _update: False)

    await bot_listener.telemetry_command(update, None)

    assert update.message.replies == [{"text": "Admin only."}]
