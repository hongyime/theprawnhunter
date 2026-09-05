import sys

sys.path.insert(0, '/app')
import asyncio

from app.core.database import db


async def backfill():
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

    print(f"Found {len(data)} media records to re-queue. Updating in chunks...")

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
    asyncio.run(backfill())
