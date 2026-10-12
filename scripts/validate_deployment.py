"""Compatibility wrapper; implementation moved to scripts/deploy/validate_deployment.py."""

from pathlib import Path
import runpy
import sys

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
target = repo_root / "scripts" / "deploy/validate_deployment.py"

if __name__ == "__main__":
    runpy.run_path(str(target), run_name="__main__")
