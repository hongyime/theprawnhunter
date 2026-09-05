"""Opt-in smoke test for a real Telegram broadcast.

This test deliberately sends one message. It never runs in the default suite.
"""

import os
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live]


@pytest.mark.asyncio
async def test_broadcaster_sends_one_message():
    if os.getenv("RUN_LIVE_BROADCASTER_TEST") != "1":
        pytest.skip("set RUN_LIVE_BROADCASTER_TEST=1 to enable the Telegram send probe")

    group_id = os.getenv("LIVE_BROADCAST_GROUP_ID")
    if not group_id:
        pytest.fail("LIVE_BROADCAST_GROUP_ID is required when the live probe is enabled")

    thread_id = int(os.getenv("LIVE_BROADCAST_THREAD_ID", "1"))

    from app.services.broadcaster_srv import BroadcasterService

    broadcaster = BroadcasterService()
    await broadcaster.send_message(
        group_id=group_id,
        thread_id=thread_id,
        msg_obj={
            "content": "The Prawn Hunter live broadcaster smoke test",
            "sender_name": "QA",
            "media_type": "text",
            "telegram_msg_id": f"qa-{uuid4().hex[:12]}",
        },
    )
