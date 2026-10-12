"""Compatibility wrapper; use ``python scripts/ops/backfill_media.py all``."""

from _legacy_backfill_entry import run

if __name__ == "__main__":
    run("all")
