"""Mute the monitor group (and its topics) for every Telethon session.

Muting is a per-account notification setting; the bot cannot do it. Run inside
a container with the sessions volume:

    docker exec -i theprawnhunter_worker /opt/venv/bin/python scripts/ops/mute_group.py
"""

from __future__ import annotations

import asyncio
import glob
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings

MUTE_UNTIL = datetime(2038, 1, 1, tzinfo=UTC)


async def mute_session(path: str) -> str:
    from telethon import TelegramClient
    from telethon.tl.functions.account import UpdateNotifySettingsRequest
    from telethon.tl.functions.messages import GetForumTopicsRequest
    from telethon.tl.types import (
        InputNotifyForumTopic,
        InputNotifyPeer,
        InputPeerNotifySettings,
    )

    client = TelegramClient(
        path.replace(".session", ""),
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "unauthorized"
        entity = await client.get_entity(settings.MONITOR_GROUP_ID)
        muted = 1
        await client(
            UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(entity),
                settings=InputPeerNotifySettings(
                    mute_until=MUTE_UNTIL, show_previews=False, silent=True
                ),
            )
        )
        try:
            topics = await client(
                GetForumTopicsRequest(
                    peer=entity, q="", offset_date=0, offset_id=0, offset_topic=0, limit=100
                )
            )
            for topic in getattr(topics, "topics", []):
                if not getattr(topic, "id", None):
                    continue
                try:
                    await client(
                        UpdateNotifySettingsRequest(
                            peer=InputNotifyForumTopic(peer=entity, top_msg_id=topic.id),
                            settings=InputPeerNotifySettings(
                                mute_until=MUTE_UNTIL, show_previews=False, silent=True
                            ),
                        )
                    )
                    muted += 1
                except Exception as error:  # noqa: BLE001
                    print(f"  topic {topic.id} skipped: {type(error).__name__}")
        except Exception as error:  # noqa: BLE001
            print(f"  topic list unavailable: {type(error).__name__}")
        return f"muted {muted} chat/topic(s)"
    except Exception as error:  # noqa: BLE001
        return f"error: {type(error).__name__} {str(error)[:60]}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def main() -> int:
    paths = sorted(glob.glob("/app/sessions/*.session"))
    for path in paths:
        print(f"{Path(path).stem[:32]:34} {await mute_session(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
