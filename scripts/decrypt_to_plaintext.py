"""
decrypt_to_plaintext.py — one-off migration to make bot_token storage uniform.

Operator explicitly accepted plaintext-at-rest (SEC-001 deferred, 2026-09-06)
and asked for uniformity — so this script decrypts every Fernet-encrypted
`discovered_credentials.bot_token` and writes back the plaintext. Rows that
are already plaintext are skipped.

Usage:
    # dry run — reports how many rows would be touched, no writes
    docker exec theprawnhunter_worker-core python /app/scripts/decrypt_to_plaintext.py --dry-run

    # apply
    docker exec theprawnhunter_worker-core python /app/scripts/decrypt_to_plaintext.py

Idempotent: safe to re-run — the `gAAAA%` filter excludes already-plaintext rows.
"""
import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("decrypt_to_plaintext")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, no writes")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    from app.core.database import db
    from app.core.security import security

    logger.info("Fetching all Fernet-encrypted rows (bot_token LIKE 'gAAAA%')...")
    # Supabase pagination — use range() slicing since we need every row.
    all_rows: list[dict] = []
    offset = 0
    page_size = 1000
    while True:
        resp = (
            db.table("discovered_credentials")
            .select("id, bot_token")
            .like("bot_token", "gAAAA%")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        page = resp.data or []
        all_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        logger.info(f"  fetched {len(all_rows)} rows so far...")

    logger.info(f"Total encrypted rows to convert: {len(all_rows)}")

    if not all_rows:
        logger.info("Nothing to do. Exit clean.")
        return 0

    if args.dry_run:
        logger.info("--dry-run set; no writes performed.")
        return 0

    converted = 0
    skipped = 0
    for i, row in enumerate(all_rows, 1):
        cred_id = row["id"]
        enc = row["bot_token"]
        try:
            plaintext = security.decrypt(enc)
        except Exception as e:
            logger.warning(f"[{i}/{len(all_rows)}] id={cred_id[:8]}... decrypt failed: {e}")
            skipped += 1
            continue

        # Sanity: shape check before writing.
        if ":" not in plaintext or not plaintext.split(":", 1)[0].isdigit():
            logger.warning(
                f"[{i}/{len(all_rows)}] id={cred_id[:8]}... decrypted value has "
                f"non-token shape (len={len(plaintext)}); skipping"
            )
            skipped += 1
            continue

        try:
            db.table("discovered_credentials").update(
                {"bot_token": plaintext}
            ).eq("id", cred_id).eq("bot_token", enc).execute()
            converted += 1
        except Exception as e:
            logger.error(f"[{i}/{len(all_rows)}] id={cred_id[:8]}... UPDATE failed: {e}")
            skipped += 1

        if i % 100 == 0:
            logger.info(f"  progress: {converted} converted, {skipped} skipped, {len(all_rows)-i} remaining")

    logger.info(f"Done. converted={converted} skipped={skipped} total={len(all_rows)}")
    return 0 if skipped == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
