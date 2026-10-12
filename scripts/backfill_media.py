"""Compatibility wrapper to the consolidated, dry-run-first media backfill CLI."""

import sys
from ops.one_off._legacy_backfill_entry import run

if __name__ == "__main__":
    run("since")
