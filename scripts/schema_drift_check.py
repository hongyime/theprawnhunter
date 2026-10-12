"""Compatibility import facade for the relocated schema drift check."""

from pathlib import Path
import sys

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
from scripts.ops import schema_drift_check as _implementation  # noqa: E402

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
