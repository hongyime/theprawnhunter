"""Compatibility facade for domain-split Celery flow tasks.

The original task names and import paths remain stable. Patching shared module
attributes through this facade also updates the implementation modules, which
preserves the long-standing test and integration seams.
"""
from types import ModuleType as _ModuleType
import sys as _sys

from app.workers.tasks.flows import common as _common
from app.workers.tasks.flows import (
    broadcast as _broadcast,
    diagnostics as _diagnostics,
    entities as _entities,
    findings as _findings,
    health as _health,
    help_text as _help_text,
    honeypot as _honeypot,
    media as _media,
    reports as _reports,
    scrape as _scrape,
    webhooks as _webhooks,
)

_IMPLEMENTATIONS = (
    _common,
    _reports,
    _diagnostics,
    _media,
    _findings,
    _broadcast,
    _health,
    _webhooks,
    _entities,
    _scrape,
    _honeypot,
    _help_text,
)
for _implementation in _IMPLEMENTATIONS:
    for _name, _value in vars(_implementation).items():
        if not _name.startswith("__") and _name not in {
            "_broadcaster",
            "_BROADCAST_RELIABILITY_COLUMNS_AVAILABLE",
            "_BROADCAST_RELIABILITY_LAST_CHECK",
        }:
            globals().setdefault(_name, _value)

class _FlowTasksFacade(_ModuleType):
    def __getattribute__(self, name):
        if name in {
            "_broadcaster",
            "_BROADCAST_RELIABILITY_COLUMNS_AVAILABLE",
            "_BROADCAST_RELIABILITY_LAST_CHECK",
        }:
            implementations = _ModuleType.__getattribute__(self, "_IMPLEMENTATIONS")
            for implementation in implementations:
                if name in vars(implementation):
                    return vars(implementation)[name]
        return _ModuleType.__getattribute__(self, name)

    def __getattr__(self, name):
        for implementation in _IMPLEMENTATIONS:
            if name in vars(implementation):
                return vars(implementation)[name]
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for implementation in _IMPLEMENTATIONS:
            if name in vars(implementation):
                setattr(implementation, name, value)

    def __dir__(self):
        return sorted(set(super().__dir__()).union(*(vars(m) for m in _IMPLEMENTATIONS)))

_sys.modules[__name__].__class__ = _FlowTasksFacade
