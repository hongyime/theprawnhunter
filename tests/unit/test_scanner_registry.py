"""Guard the scanner/heavy task registry and stale-beat expiry coverage."""

import importlib
import sys

from celery import Task

_TASK_MODULES = "app.workers.tasks"
_CELERY_APP = "app.workers.celery_app"


def _is_task_module(name):
    return name == _CELERY_APP or name.startswith(_TASK_MODULES)


def _purge_task_modules():
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if _is_task_module(name)
    }
    for name in saved:
        sys.modules.pop(name, None)
    return saved


def _restore_task_modules(saved):
    for name in [name for name in list(sys.modules) if _is_task_module(name)]:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def _load_registry():
    saved = _purge_task_modules()
    try:
        celery_module = importlib.import_module(_CELERY_APP)
        modules = [
            importlib.import_module(module_name)
            for module_name in celery_module.app.conf.imports
        ]
        return celery_module, modules
    finally:
        _restore_task_modules(saved)


def _task_names_by_module(modules):
    tasks = {}
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, Task):
                tasks[id(value)] = value
    names = {}
    for task in tasks.values():
        names.setdefault(task.name, []).append(task)
    return names


def test_scanner_and_heavy_beat_tasks_are_registered():
    celery_module, _modules = _load_registry()
    beat_schedule = celery_module.app.conf.beat_schedule

    required = {
        entry["task"]
        for entry in beat_schedule.values()
        if entry["task"].startswith(("scanner.", "heavy."))
    }
    registered = set(celery_module.app.tasks)

    missing = sorted(required - registered)
    assert not missing, f"Beat tasks are not registered in app.tasks: {missing}"


def test_no_duplicate_task_names_across_imported_modules():
    _celery_module, modules = _load_registry()
    names = _task_names_by_module(modules)

    duplicates = {
        name: len(tasks) for name, tasks in names.items() if len(tasks) > 1
    }
    assert not duplicates, f"Duplicate task names across the registry: {duplicates}"


def test_every_beat_entry_has_a_stale_expiry():
    celery_module, _modules = _load_registry()
    beat_schedule = celery_module.app.conf.beat_schedule
    expiries = celery_module._BEAT_EXPIRY_SECONDS

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
        options = beat_schedule[beat_key].get("options") or {}
        assert options.get("expires") == expires, (
            f"Beat entry {beat_key!r} did not receive expires={expires}"
        )
