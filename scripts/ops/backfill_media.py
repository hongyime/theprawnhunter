"""One safe entry point for the historical media recovery backfills.

Examples:
    python scripts/ops/backfill_media.py all --dry-run
    python scripts/ops/backfill_media.py since --since 2026-07-20T00:00:00Z --dry-run
    python scripts/ops/backfill_media.py telethon --apply

Every mode is read-only unless ``--apply`` is explicitly supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

MEDIA_TYPES = ("photo", "document", "video", "audio")
DEFAULT_SINCE = "2026-07-20T00:00:00Z"
DEFAULT_BATCH_SIZE = 50


def _media_query(db: Any, *, since: str | None, batch_size: int, offset: int | None):
    query = (
        db.table("exfiltrated_messages")
        .select("id")
        .in_("media_type", list(MEDIA_TYPES))
        .eq("is_broadcasted", True)
        .order("id")
    )
    if since:
        query = query.gte("created_at", since)
    if offset is None:
        return query.limit(batch_size)
    return query.range(offset, offset + batch_size - 1)


def _requeue_media(*, since: str | None, apply: bool, batch_size: int) -> tuple[int, int]:
    from app.core.database import db

    total_selected = 0
    total_updated = 0
    offset = 0

    while True:
        # In apply mode each successful page leaves the pending set, so read
        # from its first page again. Dry-run pages use a stable UUID ordering.
        rows = _media_query(
            db,
            since=since,
            batch_size=batch_size,
            offset=None if apply else offset,
        ).execute().data or []
        if not rows:
            break

        ids = [row["id"] for row in rows]
        total_selected += len(ids)
        if not apply:
            offset += len(ids)
            if len(ids) < batch_size:
                break
            continue

        updated = (
            db.table("exfiltrated_messages")
            .update({"is_broadcasted": False, "broadcast_claimed_at": None})
            .in_("id", ids)
            .eq("is_broadcasted", True)
            .select("id")
            .execute()
            .data
            or []
        )
        if not updated:
            raise RuntimeError("A selected media page was not updated; stopping to avoid a repeat loop")
        total_updated += len(updated)

    return total_selected, total_updated


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    all_mode = modes.add_parser("all", help="requeue all media messages already marked broadcast")
    recent = modes.add_parser("since", help="requeue broadcast media on or after a timestamp")
    recent.add_argument("--since", default=DEFAULT_SINCE, help=f"ISO timestamp (default: {DEFAULT_SINCE})")

    telethon = modes.add_parser("telethon", help="recover missing media and send it through the monitor group")

    for mode in (all_mode, recent, telethon):
        safety = mode.add_mutually_exclusive_group()
        safety.add_argument(
            "--dry-run",
            dest="apply",
            action="store_false",
            help="report eligible rows without writes or Telegram connections (default)",
        )
        safety.add_argument(
            "--apply",
            dest="apply",
            action="store_true",
            help="perform the selected database updates; telethon mode can send Telegram messages",
        )
        mode.set_defaults(apply=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.mode == "telethon":
        from scripts.ops.one_off._backfill_telethon import main as telethon_backfill

        if not args.apply:
            print("Dry run: no Telegram clients will be started and no messages will be sent.")
        asyncio.run(telethon_backfill(apply_changes=args.apply))
        return 0

    since = args.since if args.mode == "since" else None
    selected, updated = _requeue_media(
        since=since,
        apply=args.apply,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    if args.apply:
        print(f"Applied media requeue: selected={selected}, updated={updated}.")
    else:
        print(f"Dry run: {selected} media messages would be requeued; no changes were made. Pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
