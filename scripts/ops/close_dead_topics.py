"""Close forum topics whose bot token is dead (no username resolvable).

Closed topics render greyed-out in Telegram, which is the clearest signal that
the credential is dead while keeping the evidence history readable.

    docker exec -i theprawnhunter_worker /opt/venv/bin/python scripts/ops/close_dead_topics.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings
from app.core.database import db
from app.infrastructure.database import execute_async


async def run(apply: bool) -> int:
    response = await execute_async(
        db.table("discovered_credentials")
        .select("id,meta,bot_username")
        .is_("meta->>bot_username", "null")
        .is_("bot_username", "null")
    )
    rows = response.data or []
    dead = [row for row in rows if (row.get("meta") or {}).get("topic_id")]
    print(f"{'Closing' if apply else 'Would close'} {len(dead)} dead topic(s) of {len(rows)} dead credential(s)")
    if not apply:
        return 0

    from telegram import Bot

    bot = Bot(settings.bot_tokens[0])
    closed = failed = already_closed = 0
    async with bot:
        import asyncio

        for row in dead:
            topic_id = int((row.get("meta") or {})["topic_id"])
            for attempt in range(3):
                try:
                    await bot.close_forum_topic(
                        chat_id=settings.MONITOR_GROUP_ID, message_thread_id=topic_id
                    )
                    closed += 1
                    break
                except Exception as error:  # noqa: BLE001
                    retry_after = int(getattr(error, "retry_after", 0) or 0)
                    if retry_after and attempt < 2:
                        await asyncio.sleep(retry_after + 1)
                        continue
                    if "TOPIC_NOT_MODIFIED" in str(error):
                        already_closed += 1
                    else:
                        failed += 1
                        print(f"  {row['id'][:8]}: {type(error).__name__}")
                    break
            await asyncio.sleep(30)
    print(f"closed={closed} already_closed={already_closed} failed={failed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    sys.exit(main())
