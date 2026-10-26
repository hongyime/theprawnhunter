"""Evaluate existing messages against the watchlist rules (historical backfill).

New captures are evaluated as they arrive; this scans what is already stored.
Cooldowns and the (rule_id, message_id) dedupe still apply, so it is safe to
re-run. Alerts are OFF by default to avoid a Telegram flood.

    docker exec -i theprawnhunter_worker /opt/venv/bin/python scripts/ops/backfill_watchlists.py --limit 500
    ... --apply --alert
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.database import db
from app.infrastructure.database import execute_async


async def run(limit: int, since_days: int | None, apply: bool, alert: bool) -> int:
    query = (
        db.table("exfiltrated_messages")
        .select("id,credential_id,content,created_at")
        .order("created_at", desc=True)
        .limit(limit)
    )
    if since_days is not None:
        from datetime import UTC, datetime, timedelta

        query = query.gte(
            "created_at", (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
        )
    rows = await execute_async(query)
    messages = [row for row in (rows.data or []) if (row.get("content") or "").strip()]
    print(f"{'Evaluating' if apply else 'Would evaluate'} {len(messages)} message(s)")

    from app.services import watchlists

    hits = 0
    for index, row in enumerate(messages, 1):
        if not apply:
            continue
        matches = await watchlists.evaluate(
            row["content"],
            message_id=row["id"],
            credential_id=row.get("credential_id"),
        )
        hits += len(matches)
        if matches and alert:
            from app.services.watchlist_alerts import alert_on_matches

            await alert_on_matches(
                row["content"],
                message_id=row["id"],
                credential_id=row.get("credential_id"),
            )
        if index % 100 == 0:
            print(f"  {index}/{len(messages)} scanned, {hits} hit(s)")
    print(f"hits={hits}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--since-days", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--alert", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.limit, args.since_days, args.apply, args.alert))


if __name__ == "__main__":
    sys.exit(main())
