"""The shared media backfill CLI must require an explicit apply choice."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "backfill_media.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("media_backfill_cli", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("mode_args", [["all"], ["since"], ["telethon"]])
def test_all_modes_default_to_dry_run(mode_args):
    args = _load_cli()._parser().parse_args(mode_args)

    assert args.apply is False


@pytest.mark.parametrize("mode_args", [["all"], ["since"], ["telethon"]])
def test_apply_requires_an_explicit_flag(mode_args):
    args = _load_cli()._parser().parse_args([*mode_args, "--apply"])

    assert args.apply is True


def test_dry_run_and_apply_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _load_cli()._parser().parse_args(["telethon", "--dry-run", "--apply"])


def test_all_mode_passes_read_only_default(monkeypatch, capsys):
    cli = _load_cli()
    seen = {}
    monkeypatch.setattr(cli, "_requeue_media", lambda **kwargs: seen.update(kwargs) or (3, 0))

    assert cli.main(["all"]) == 0
    assert seen == {"since": None, "apply": False, "batch_size": cli.DEFAULT_BATCH_SIZE}
    assert "no changes were made" in capsys.readouterr().out


def test_telethon_mode_passes_read_only_default(monkeypatch, capsys):
    cli = _load_cli()
    calls = []

    async def fake_backfill(*, apply_changes):
        calls.append(apply_changes)

    module_name = "scripts.ops.one_off._backfill_telethon"
    fake_module = ModuleType(module_name)
    fake_module.main = fake_backfill
    monkeypatch.setitem(sys.modules, module_name, fake_module)

    assert cli.main(["telethon"]) == 0
    assert calls == [False]
    assert "no messages will be sent" in capsys.readouterr().out
