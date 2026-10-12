import argparse
import sys

sys.path.insert(0, '/app')
import asyncio  # noqa: E402 — intentional deferred import

from app.core.database import db  # noqa: E402 — intentional deferred import


async def backfill(apply_changes: bool = False):
    print("Fetching missed media since July 20th...")
    res = db.table('exfiltrated_messages') \
        .select('id, telegram_msg_id') \
        .gte('created_at', '2026-07-20T00:00:00Z') \
        .in_('media_type', ['photo', 'document', 'video', 'audio']) \
        .eq('is_broadcasted', True) \
        .execute()

    data = res.data or []
    if not data:
        print("No eligible media found to backfill.")
        return

    print(f"Found {len(data)} media records to requeue.")

    if not apply_changes:
        print("Dry run only; pass --apply to requeue these media records.")
        return

    print("Applying requeue updates in chunks...")

    ids = [row['id'] for row in data]
    chunk_size = 50
    total_updated = 0

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i+chunk_size]
        update_res = db.table('exfiltrated_messages') \
            .update({
                'is_broadcasted': False,
                'broadcast_claimed_at': None
            }) \
            .in_('id', chunk) \
            .execute()

        updated_data = update_res.data or []
        total_updated += len(updated_data)

    print(f"Successfully pushed {total_updated} missing media items back into the queue!")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Requeue recent broadcast media records.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write requeue updates to the database (default: report only)",
    )
    args = parser.parse_args()
    asyncio.run(backfill(apply_changes=args.apply))
