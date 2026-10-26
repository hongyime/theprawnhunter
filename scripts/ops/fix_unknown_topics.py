"""Resolve or rename topics stuck at "@unknown / <bot_id>".

For each credential missing meta.bot_username we try getMe with its stored
token. Valid tokens get their real @username and the topic is renamed to
"@user / id". Dead tokens (getMe fails) get the honest "bot_<id>" name.

Dry run by default; pass --apply to write and rename.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from app.core.config import settings
from app.core.database import db
from app.core.security import security
from app.infrastructure.database import execute_async


async def _get_me(token: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        payload = response.json()
        return payload.get("result") if payload.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


async def run(limit: int, apply: bool) -> int:
    response = await execute_async(
        db.table("discovered_credentials")
        .select("id,bot_token,meta,bot_username")
        .is_("meta->>bot_username", "null")
        .limit(limit)
    )
    rows = response.data or []
    print(f"{'Fixing' if apply else 'Would fix'} {len(rows)} credential(s)")

    from app.workers.tasks.flow_tasks import get_broadcaster

    broadcaster = get_broadcaster() if apply else None
    resolved = renamed_dead = 0
    for row in rows:
        await asyncio.sleep(1.2)
        meta = dict(row.get("meta") or {})
        topic_id = meta.get("topic_id")
        bot_id = meta.get("bot_id")
        try:
            token = security.decrypt(row["bot_token"]) if row.get("bot_token") else ""
        except Exception:  # noqa: BLE001
            token = ""
        column_username = row.get("bot_username")
        me = None
        if not column_username and token:
            me = await _get_me(token)
        if column_username:
            if apply:
                meta["bot_username"] = column_username
                await execute_async(
                    db.table("discovered_credentials").update({"meta": meta}).eq("id", row["id"])
                )
                if topic_id and bot_id:
                    await broadcaster.rename_topic(
                        settings.MONITOR_GROUP_ID, int(topic_id), f"@{column_username} / {bot_id}"
                    )
            resolved += 1
        elif me and me.get("username"):
            if apply:
                meta["bot_username"] = me["username"]
                meta["bot_name"] = me.get("first_name")
                bot_id = bot_id or str(me.get("id") or "")
                await execute_async(
                    db.table("discovered_credentials").update({"meta": meta}).eq("id", row["id"])
                )
                if topic_id and bot_id:
                    await broadcaster.rename_topic(
                        settings.MONITOR_GROUP_ID, int(topic_id), f"@{me['username']} / {bot_id}"
                    )
            resolved += 1
        elif bot_id:
            if apply and topic_id:
                await broadcaster.rename_topic(
                    settings.MONITOR_GROUP_ID, int(topic_id), f"bot_{bot_id}"
                )
            renamed_dead += 1
    print(f"resolved={resolved} dead-renamed={renamed_dead}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.limit, args.apply))


if __name__ == "__main__":
    sys.exit(main())
