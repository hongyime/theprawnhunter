"""List Telethon sessions: phone, username, validity and anonymity.

Run inside a container that has the sessions volume mounted, e.g.
    docker exec -i theprawnhunter_worker /opt/venv/bin/python scripts/ops/session_inventory.py
"""

from __future__ import annotations

import asyncio
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings

SESSIONS_GLOB = "/app/sessions/*.session"


async def main() -> int:
    from telethon import TelegramClient
    from telethon.tl.functions.channels import GetParticipantRequest

    paths = sorted(glob.glob(SESSIONS_GLOB))
    if not paths:
        print("no session files found")
        return 1

    print(f"{'phone':16} {'username':18} {'valid':6} {'anon':6} {'post':6}")
    for path in paths:
        phone = Path(path).stem.split("_")[1] if "_" in Path(path).stem else "?"
        client = TelegramClient(
            path.replace(".session", ""),
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
        )
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
            if not authorized:
                print(f"{phone:16} {'-':18} {'NO':6} {'-':6} {'-':6}")
                continue
            me = await client.get_me()
            anonymous = post = "-"
            try:
                entity = await client.get_entity(settings.MONITOR_GROUP_ID)
                part = (
                    await client(GetParticipantRequest(channel=entity, participant=me.id))
                ).participant
                rights = getattr(part, "admin_rights", None)
                anonymous = str(bool(getattr(rights, "anonymous", False)))
                post = str(bool(getattr(rights, "post_messages", False)))
            except Exception as error:  # noqa: BLE001
                anonymous = f"err:{type(error).__name__[:8]}"
            print(f"{phone:16} {me.username or me.id!s:18} {'yes':6} {anonymous:6} {post:6}")
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
