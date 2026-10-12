"""Compatibility entry point for the historical backfill script names."""

from __future__ import annotations

import sys
from pathlib import Path


def run(mode: str) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo_root))
    from scripts.ops.backfill_media import main

    raise SystemExit(main([mode, *sys.argv[1:]]))
