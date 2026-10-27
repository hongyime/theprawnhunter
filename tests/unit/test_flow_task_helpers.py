"""Guard against tasks calling ``_run_sync`` without importing it.

``flows/common.py`` re-exports most helpers but not ``_run_sync``; a missing
import only fails at runtime, inside Celery, where it is easy to miss. This
test caught two regressions, so it stays.
"""

from __future__ import annotations

import ast
import pathlib

FLOWS_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "workers" / "tasks" / "flows"


def test_every_flow_module_using_run_sync_imports_it():
    offenders = []
    for path in sorted(FLOWS_DIR.glob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        uses = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_run_sync"
            for node in ast.walk(tree)
        )
        if not uses:
            continue
        imported = any(
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "_run_sync" for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imported:
            offenders.append(path.name)
    assert offenders == [], f"missing `from app.workers.celery_app import _run_sync`: {offenders}"
