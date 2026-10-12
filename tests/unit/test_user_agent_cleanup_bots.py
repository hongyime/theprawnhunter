from types import SimpleNamespace

import pytest

from app.services.user_agent_srv import UserAgentService


@pytest.mark.asyncio
async def test_cleanup_bots_preserves_whitelisted_and_admin_bots(monkeypatch):
    from telethon import tl
    from telethon.tl.functions.channels import EditBannedRequest, GetParticipantRequest

    class _AdminParticipant:
        pass

    monkeypatch.setattr(tl.types, "ChannelParticipantAdmin", _AdminParticipant)
    monkeypatch.setattr(
        tl.types,
        "ChannelParticipantCreator",
        type("_CreatorParticipant", (), {}),
    )

    users = [
        SimpleNamespace(id=100, username="keep_bot", bot=True, is_self=False),
        SimpleNamespace(id=200, username="admin_bot", bot=True, is_self=False),
        SimpleNamespace(id=300, username="remove_bot", bot=True, is_self=False),
        SimpleNamespace(id=400, username="unknown_bot", bot=True, is_self=False),
        SimpleNamespace(id=500, username="human", bot=False, is_self=False),
        SimpleNamespace(id=600, username="self_bot", bot=True, is_self=True),
    ]

    class _FakeClient:
        def __init__(self):
            self.edit_requests = []

        async def get_entity(self, _target):
            return SimpleNamespace(title="monitor")

        async def __call__(self, request):
            if isinstance(request, GetParticipantRequest):
                participant_id = request.participant.id
                if participant_id == 200:
                    return SimpleNamespace(participant=_AdminParticipant())
                if participant_id == 400:
                    raise RuntimeError("participant lookup failed")
                return SimpleNamespace(participant=object())
            if isinstance(request, EditBannedRequest):
                self.edit_requests.append(request)
                return SimpleNamespace()
            raise AssertionError(f"Unexpected Telegram request: {type(request).__name__}")

        async def _iter_participants(self):
            for user in users:
                yield user

        def iter_participants(self, _entity):
            return self._iter_participants()

    service = UserAgentService()
    client = _FakeClient()
    disconnected = []

    async def start():
        return True

    async def disconnect():
        disconnected.append(True)

    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(service, "_disconnect", disconnect)
    service.client = client

    removed = await service.cleanup_bots(-100123, [100, "@other_kept_bot"])

    assert removed == 1
    assert [request.participant.id for request in client.edit_requests] == [300, 300]
    assert [request.banned_rights.view_messages for request in client.edit_requests] == [True, False]
    assert disconnected == [True]
