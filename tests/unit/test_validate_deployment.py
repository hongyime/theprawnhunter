"""The deployment validator must import the symbols that the app exposes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

VALIDATOR = Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "validate_deployment.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("deployment_validator", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_probes_load_current_symbols(capsys):
    validator = _load_validator()
    checks = (
        validator.test_core_imports,
        validator.test_service_imports,
        validator.test_task_imports,
        validator.test_api_imports,
        validator.test_helper_imports,
    )

    assert all(check() for check in checks)
    output = capsys.readouterr().out
    assert "import failed" not in output


def test_feature_and_security_probes_use_current_apis(capsys):
    validator = _load_validator()

    assert validator.test_new_features()
    assert validator.test_security()
    output = capsys.readouterr().out
    assert "failed" not in output.lower()
