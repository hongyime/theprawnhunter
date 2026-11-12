from types import SimpleNamespace

import pytest

from app.services._bot_listener import commands, login


def _update(chat_type: str):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(type=chat_type),
    )


@pytest.mark.asyncio
async def test_private_start_uses_the_existing_login_entrypoint(monkeypatch):
    async def tracked(*_args):
        return {"status": "tracked"}

    async def start_login(update, context):
        assert update.effective_chat.type == "private"
        assert context is not None
        return "wait_phone"

    monkeypatch.setattr("app.services.engagement.track_owned_bot_start", tracked)
    monkeypatch.setattr(login, "starthunter", start_login)
    monkeypatch.setattr(commands, "_reply_and_delete", lambda *_args, **_kwargs: pytest.fail("must delegate"))

    assert await commands.start(_update("private"), SimpleNamespace()) == "wait_phone"


@pytest.mark.asyncio
async def test_group_start_keeps_the_existing_instruction(monkeypatch):
    replies = []

    async def tracked(*_args):
        return {"status": "tracked"}

    async def reply(_update, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr("app.services.engagement.track_owned_bot_start", tracked)
    monkeypatch.setattr(commands, "is_admin", lambda _update: False)
    monkeypatch.setattr(commands, "_reply_and_delete", reply)

    assert await commands.start(_update("group"), SimpleNamespace()) is None
    assert replies == ["Welcome. Use /starthunter in a private chat to login."]
