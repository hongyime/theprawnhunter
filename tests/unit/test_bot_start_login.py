from types import SimpleNamespace

import pytest

from app.services._bot_listener import commands, login, runtime


def _update(chat_type: str):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(type=chat_type),
        message=SimpleNamespace(text="/start"),
    )


@pytest.mark.asyncio
async def test_reentering_private_start_cleans_the_old_attempt(monkeypatch):
    update = _update("private")
    update.effective_chat.id = 99
    active_client = object()
    context = SimpleNamespace(
        user_data={
            "bot_messages": [7, 8],
            "client": active_client,
            "temp_session_path": "/tmp/login",
            "login_state": "WAITING_FOR_CODE",
        },
        args=[],
        bot=SimpleNamespace(id=1),
    )
    calls = []

    async def tracked(*_args):
        return {"status": "tracked"}

    async def cleanup(context):
        calls.append(("cleanup", context.user_data["client"]))

    async def wipe(context, chat_id, message_ids):
        calls.append(("wipe", chat_id, message_ids))

    async def start_flow(update, context, *, skip_rate_limit):
        calls.append(("start", skip_rate_limit))
        return "wait_phone"

    monkeypatch.setattr("app.services.engagement.track_owned_bot_start", tracked)
    monkeypatch.setattr(login, "_cleanup_login_attempt", cleanup)
    monkeypatch.setattr(login, "_wipe_conversation", wipe)
    monkeypatch.setattr(login, "starthunter", start_flow)

    assert await login.start_login(update, context) == "wait_phone"
    assert calls == [
        ("cleanup", active_client),
        ("wipe", 99, [7, 8]),
        ("start", True),
    ]
    assert context.user_data == {}


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


def test_login_commands_are_owned_by_one_reentrant_conversation_handler():
    application = runtime._build_application("123456:test-token")
    conversation = next(
        handler
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, runtime.ConversationHandler)
    )

    assert conversation.allow_reentry is True
    assert conversation.entry_points[0].commands == frozenset({"start", "starthunter"})
