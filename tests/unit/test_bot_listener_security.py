from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import bot_listener


class _FakeMessage:
    def __init__(self, *, text=None, message_id=1):
        self.text = text
        self.message_id = message_id
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return SimpleNamespace(message_id=100 + len(self.replies))


@pytest.mark.asyncio
async def test_getfile_rejects_group_chats_before_authorization(monkeypatch):
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100123, type="group"),
        message=_FakeMessage(),
    )

    monkeypatch.setattr(
        bot_listener,
        "is_admin",
        lambda _update: pytest.fail("group chat should be rejected before authorization"),
    )

    await bot_listener.getfile_command(update, SimpleNamespace(args=["123"], user_data={}))

    assert update.message.replies == [
        {"text": "For security, /getfile only works in a private chat with me."}
    ]


@pytest.mark.asyncio
async def test_handle_phone_disconnects_and_removes_temp_session_when_code_request_fails(
    monkeypatch,
):
    created_paths = []

    class _FakeClient:
        def __init__(self):
            self.connected = False
            self.disconnect_count = 0
            self.session_base = None

        async def connect(self):
            self.connected = True
            for suffix in (".session", ".session-wal", ".session-shm"):
                path = Path(self.session_base + suffix)
                path.write_text("temporary session data")
                created_paths.append(path)

        async def send_code_request(self, _phone):
            raise RuntimeError("Telegram request failed")

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.disconnect_count += 1
            self.connected = False

    client = _FakeClient()

    def make_client(session_base, *_args):
        client.session_base = session_base
        return client

    async def no_op_wipe(*_args):
        return None

    monkeypatch.setattr(bot_listener, "TelegramClient", make_client)
    monkeypatch.setattr(bot_listener, "_wipe_conversation", no_op_wipe)

    message = _FakeMessage(text="+15551234567")
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=123, type="private"),
        message=message,
    )
    context = SimpleNamespace(user_data={"bot_messages": []})

    result = await bot_listener.handle_phone(update, context)

    assert result == bot_listener.ConversationHandler.END
    assert client.disconnect_count == 1
    assert all(not path.exists() for path in created_paths)
