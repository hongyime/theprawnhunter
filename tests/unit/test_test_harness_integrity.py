"""Static checks that keep pytest probes from silently becoming false-green."""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]


def test_async_pytest_functions_declare_an_asyncio_runner():
    missing = []
    for path in TESTS.rglob("test_*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_has_runner = "pytestmark" in source and (
            "pytest.mark.asyncio" in source or "pytest.mark.anyio" in source
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("test"):
                continue
            decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
            if not module_has_runner and not decorators.intersection(
                {"pytest.mark.asyncio", "pytest.mark.anyio"}
            ):
                missing.append(f"{path.relative_to(TESTS)}::{node.name}")

    assert missing == [], f"async tests without a pytest runner: {missing}"


def test_live_probe_modules_skip_explicitly_and_never_exit_the_process():
    live_modules = []
    for path in TESTS.rglob("test_*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        is_live = any(
            isinstance(node, ast.Attribute) and ast.unparse(node) == "pytest.mark.live"
            for node in ast.walk(tree)
        )
        if not is_live:
            continue
        live_modules.append(path.relative_to(TESTS).as_posix())
        assert "pytest.skip(" in source, f"{path} has no explicit opt-in skip"
        exits = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "sys.exit"
        ]
        assert exits == [], f"{path} exits pytest instead of reporting failure"

    assert live_modules, "no live probes were discovered"
