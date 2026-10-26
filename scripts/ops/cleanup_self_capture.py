"""Remove broadcasts that came from the monitor group itself, plus duplicate topics.

Dry-run by default. Pass --apply to delete. Requires a usable Telethon session;
if every session is in flood-wait cooldown the script exits with a clear message.

Usage:
    python scripts/ops/cleanup_self_capture.py
    python scripts/ops/cleanup_self_capture.py --apply
    python scripts/ops/cleanup_self_capture.py --apply --topic-id 1133933
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings
from app.services.user_agent_srv import user_agent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete instead of report")
    parser.add_argument("--limit", type=int, default=200, help="max messages to scan")
    parser.add_argument(
        "--marker",
        default="[Group: The Prawn Hunter",
        help="substring identifying a self-captured broadcast",
    )
    parser.add_argument(
        "--topic-id",
        type=int,
        default=None,
        help="optionally delete an empty duplicate forum topic by id",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    if not await user_agent.start():
        print("No usable Telethon session (all sessions may be cooling down).")
        return 2

    try:
        entity = await user_agent.client.get_entity(settings.MONITOR_GROUP_ID)
        found = await user_agent.client.get_messages(
            entity, search=args.marker, limit=args.limit
        )
        print(f"matched {len(found)} self-captured message(s)")
        for message in found[:20]:
            preview = (message.message or "").replace("\n", " ")[:70]
            print(f"  {message.id}: {preview}")

        if args.apply and found:
            await user_agent.client.delete_messages(entity, [m.id for m in found])
            print(f"deleted {len(found)} message(s)")

        if args.topic_id:
            topics = await user_agent.client.get_messages(
                entity, limit=5, reply_to=args.topic_id
            )
            real = [m for m in topics if not getattr(m, "action", None)]
            print(f"topic {args.topic_id}: {len(topics)} message(s), {len(real)} real")
            if args.apply and len(real) == 0:
                from telethon.tl.functions.messages import DeleteTopicHistoryRequest

                await user_agent.client(
                    DeleteTopicHistoryRequest(peer=entity, top_msg_id=args.topic_id)
                )
                print(f"deleted empty topic {args.topic_id}")
            elif args.apply:
                print("topic is not empty; left untouched")
        return 0
    finally:
        await user_agent._disconnect()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.apply:
        print("Dry run. Pass --apply to delete.\n")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
