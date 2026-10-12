"""Compatibility wrapper; implementation moved to scripts/ops/one_off/rotate_credentials.py."""

from pathlib import Path
import runpy
import sys

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
target = repo_root / "scripts" / "ops/one_off/rotate_credentials.py"

if __name__ == "__main__":
    runpy.run_path(str(target), run_name="__main__")
