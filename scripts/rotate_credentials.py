#!/usr/bin/env python3
"""Re-encrypt every discovered_credentials.bot_token under the current
ENCRYPTION_KEY. Requires ENCRYPTION_KEY_LEGACY to be set (comma-separated
list of previous keys). Idempotent — skips rows that decrypt cleanly and
encrypt to the same ciphertext (already migrated).

Usage:
    docker exec theprawnhunter_worker-core python scripts/rotate_credentials.py --batch-size 100 --dry-run
    docker exec theprawnhunter_worker-core python scripts/rotate_credentials.py --batch-size 100

Runbook:
    1. Generate new Fernet key.
    2. Move current ENCRYPTION_KEY value to ENCRYPTION_KEY_LEGACY in .env
       (comma-append if legacy already set).
    3. Set ENCRYPTION_KEY to the new key.
    4. Restart workers — decryption still works via MultiFernet fallback.
    5. Run this script — it re-encrypts each row under the new primary.
    6. Verify no rows still under legacy key (count via decrypt attempts).
    7. Remove the legacy key from ENCRYPTION_KEY_LEGACY.
"""
import argparse
import os
import sys

# Add project root to path for `python scripts/rotate_credentials.py` from any cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import MultiFernet

from app.core.database import db
from app.core.security import security


def _is_multifernet() -> bool:
    return isinstance(security.fernet, MultiFernet)


def _current_primary_key() -> str:
    """Return the raw primary key bytes as a printable string, for detecting
    whether a row is already encrypted with it. We compare by decrypt-then-
    encrypt trip: if the round-trip ciphertext differs from the stored one,
    it was under a legacy key and needs rotation."""
    from app.core.config import settings
    return settings.ENCRYPTION_KEY


def rotate_batch(batch_size: int, dry_run: bool) -> tuple[int, int, int]:
    """Returns (rotated, skipped, failed)."""
    if not _is_multifernet():
        print(
            "SecurityService is single-key mode (no ENCRYPTION_KEY_LEGACY set). "
            "Nothing to rotate — .rotate() is a no-op. Set ENCRYPTION_KEY_LEGACY "
            "in .env before running this script."
        )
        return 0, 0, 0

    rotated = skipped = failed = 0
    offset = 0

    while True:
        res = (
            db.table("discovered_credentials")
            .select("id, bot_token")
            .not_.is_("bot_token", "null")
            .range(offset, offset + batch_size - 1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            break

        for row in rows:
            row_id = row["id"]
            ct = row.get("bot_token")
            if not ct:
                continue
            try:
                new_ct = security.fernet.rotate(ct.encode()).decode()
            except Exception as e:
                print(f"[FAIL] {row_id}: {type(e).__name__}: {e}")
                failed += 1
                continue

            if new_ct == ct:
                # Already under primary key
                skipped += 1
                continue

            if dry_run:
                rotated += 1
                continue

            try:
                db.table("discovered_credentials").update({"bot_token": new_ct}).eq("id", row_id).execute()
                rotated += 1
            except Exception as e:
                print(f"[FAIL update] {row_id}: {type(e).__name__}: {e}")
                failed += 1

        offset += batch_size
        print(f"  progress: offset={offset} rotated={rotated} skipped={skipped} failed={failed}")

    return rotated, skipped, failed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--dry-run", action="store_true", help="Report only, don't write")
    args = p.parse_args()

    print(f"MultiFernet: {_is_multifernet()}")
    print(f"Dry-run: {args.dry_run}")
    print(f"Batch size: {args.batch_size}")
    print()

    rotated, skipped, failed = rotate_batch(args.batch_size, args.dry_run)

    print()
    print("=== SUMMARY ===")
    print(f"Rotated: {rotated}")
    print(f"Skipped (already under primary): {skipped}")
    print(f"Failed: {failed}")

    if args.dry_run:
        print("\n(dry-run — no writes performed)")


if __name__ == "__main__":
    main()
