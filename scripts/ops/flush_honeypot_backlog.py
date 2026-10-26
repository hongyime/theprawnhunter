"""Re-send honeypot messages that were captured while delivery was paused.

Honeypot rows use negative telegram_msg_id and never enter the normal backlog
loop, so they are flushed here through the same broadcaster (pins included) and
then marked broadcasted.

Run inside a container with the app + network:
    docker exec -i theprawnhunter_worker /opt/venv/bin/python scripts/ops/flush_honeypot_backlog.py --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings
from app.core.database import db
from app.infrastructure.database import execute_async


async def run(limit: int, apply: bool) -> int:
    response = await execute_async(
        db.table("exfiltrated_messages")
        .select("*, discovered_credentials!inner(meta)")
        .eq("is_broadcasted", False)
        .lt("telegram_msg_id", 0)
        .order("created_at", desc=False)
        .limit(limit)
    )
    rows = response.data or []
    print(f"{'Sending' if apply else 'Would send'} {len(rows)} honeypot message(s)")
    if not apply or not rows:
        return 0

    from app.workers.tasks.flow_tasks import get_broadcaster

    broadcaster = get_broadcaster()
    sent = failed = 0
    for row in rows:
        meta = (row.get("discovered_credentials") or {}).get("meta") or {}
        topic_id = meta.get("topic_id")
        if not topic_id:
            failed += 1
            continue
        try:
            message_id = await broadcaster.send_message(settings.MONITOR_GROUP_ID, int(topic_id), row)
        except Exception as error:  # noqa: BLE001
            print(f"  {row['id']}: {type(error).__name__}")
            failed += 1
            continue
        if not message_id:
            failed += 1
            continue
        await execute_async(
            db.table("exfiltrated_messages")
            .update(
                {
                    "is_broadcasted": True,
                    "broadcasted_at": datetime.now(UTC).isoformat(),
                    "broadcast_message_id": message_id,
                    "broadcast_status": "sent",
                }
            )
            .eq("id", row["id"])
        )
        sent += 1
    print(f"sent={sent} failed={failed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.limit, args.apply))


if __name__ == "__main__":
    sys.exit(main())
