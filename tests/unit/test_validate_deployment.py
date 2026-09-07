"""Guards against deployment validation becoming an unconditional success."""

from __future__ import annotations

import ast
from pathlib import Path

VALIDATOR = Path(__file__).resolve().parents[2] / "scripts" / "validate_deployment.py"


def test_import_checks_execute_real_imports():
    tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    for name in (
        "test_core_imports",
        "test_service_imports",
        "test_task_imports",
        "test_api_imports",
        "test_helper_imports",
    ):
        imports = (
            node
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Import | ast.ImportFrom)
        )
        assert next(imports, None) is not None, f"{name} does not validate any import"
