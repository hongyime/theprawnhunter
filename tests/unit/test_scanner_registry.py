"""Guard the scanner/heavy task registry and stale-beat expiry coverage.

The registry is inspected in a **subprocess with a fresh interpreter**. Loading
the real Celery app and every ``app.conf.imports`` module in-process would
replace (and later restore) entries in ``sys.modules``, leaving task-module
identity inconsistent for every test that runs afterwards. That process-global
mutation made later modules — e.g. ``test_watchlist_alerts.py``'s string-target
monkeypatch of ``app.workers.tasks.flow_tasks.get_broadcaster`` — resolve the
wrong module object and fail only in full-suite order.

Running the import in a disposable interpreter keeps this test's assertions
identical while leaving the pytest process's module table untouched.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Mock settings values only — identical placeholders to tests/unit/conftest.py.
# Passed through the child environment, never through the command line.
_MOCK_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_KEY": "mock-key",
    "SUPABASE_SERVICE_ROLE_KEY": "mock-service-role-key",
    "REDIS_URL": "redis://localhost:6379/0",
    "ENCRYPTION_KEY": "B" * 43 + "=",
    "MONITOR_BOT_TOKEN": "123:ABC",
    "MONITOR_GROUP_ID": "-100123",
    "MONITOR_API_KEY": "test-monitor-key-for-pytest",
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "abc",
}

# Imports the real app + every autodiscovered task module, then dumps a JSON
# snapshot of the registry. stdout is redirected during imports so incidental
# import-time logging cannot corrupt the payload.
_SNAPSHOT_SCRIPT = r"""
import contextlib
import importlib
import io
import json
import sys

with contextlib.redirect_stdout(io.StringIO()):
    celery_module = importlib.import_module("app.workers.celery_app")
    modules = [
        importlib.import_module(name)
        for name in celery_module.app.conf.imports
    ]
    from celery import Task

    task_objects = {}
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, Task):
                task_objects[id(value)] = value

names = {}
for task in task_objects.values():
    names.setdefault(task.name, []).append(task)

beat = {}
for key, entry in celery_module.app.conf.beat_schedule.items():
    options = entry.get("options") or {}
    beat[key] = {"task": entry["task"], "expires": options.get("expires")}

payload = {
    "registered": sorted(celery_module.app.tasks),
    "beat": beat,
    "expiries": dict(celery_module._BEAT_EXPIRY_SECONDS),
    "duplicates": {name: len(tasks) for name, tasks in names.items() if len(tasks) > 1},
}
json.dump(payload, sys.stdout)
"""

_SNAPSHOT = None


def _registry_snapshot():
    """Import the registry in a fresh interpreter and return its JSON snapshot."""
    global _SNAPSHOT
    if _SNAPSHOT is not None:
        return _SNAPSHOT

    env = os.environ.copy()
    for key, value in _MOCK_ENV.items():
        env.setdefault(key, value)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(_PROJECT_ROOT)
        if not existing_pythonpath
        else str(_PROJECT_ROOT) + os.pathsep + existing_pythonpath
    )

    completed = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_SCRIPT],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Registry snapshot subprocess failed "
            f"(exit {completed.returncode}):\n{completed.stderr}"
        )

    try:
        _SNAPSHOT = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"Registry snapshot subprocess emitted invalid JSON: {exc}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        ) from exc
    return _SNAPSHOT


def test_scanner_and_heavy_beat_tasks_are_registered():
    snapshot = _registry_snapshot()
    beat_schedule = snapshot["beat"]

    required = {
        entry["task"]
        for entry in beat_schedule.values()
        if entry["task"].startswith(("scanner.", "heavy."))
    }
    registered = set(snapshot["registered"])

    missing = sorted(required - registered)
    assert not missing, f"Beat tasks are not registered in app.tasks: {missing}"


def test_no_duplicate_task_names_across_imported_modules():
    snapshot = _registry_snapshot()

    assert not snapshot["duplicates"], (
        "Duplicate task names across the registry: " + str(snapshot["duplicates"])
    )


def test_every_beat_entry_has_a_stale_expiry():
    snapshot = _registry_snapshot()
    beat_schedule = snapshot["beat"]
    expiries = snapshot["expiries"]

    missing = sorted(set(beat_schedule) - set(expiries))
    assert not missing, (
        "Beat schedule entry is missing a _BEAT_EXPIRY_SECONDS entry: "
        + ", ".join(missing)
    )

    stray = sorted(set(expiries) - set(beat_schedule))
    assert not stray, (
        "_BEAT_EXPIRY_SECONDS names beat entries that do not exist: "
        + ", ".join(stray)
    )

    for beat_key, expires in expiries.items():
        assert beat_schedule[beat_key]["expires"] == expires, (
            f"Beat entry {beat_key!r} did not receive expires={expires}"
        )
