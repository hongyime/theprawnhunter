"""Compatibility facade for provider-specific scanner Celery tasks."""
from types import ModuleType as _ModuleType
import sys as _sys

from app.workers.tasks.scanners import common as _common
from app.workers.tasks.scanners.providers import shodan as _provider_0
from app.workers.tasks.scanners.providers import urlscan as _provider_1
from app.workers.tasks.scanners.providers import github as _provider_2
from app.workers.tasks.scanners.providers import fofa as _provider_3
from app.workers.tasks.scanners.providers import gitlab as _provider_4
from app.workers.tasks.scanners.providers import bitbucket as _provider_5
from app.workers.tasks.scanners.providers import github_gist as _provider_6
from app.workers.tasks.scanners.providers import grepapp as _provider_7
from app.workers.tasks.scanners.providers import publicwww as _provider_8
from app.workers.tasks.scanners.providers import pastebin as _provider_9
from app.workers.tasks.scanners.providers import rentry as _provider_10
from app.workers.tasks.scanners.providers import hastebin as _provider_11
from app.workers.tasks.scanners.providers import exa as _provider_12
from app.workers.tasks.scanners.providers import cold as _provider_13
from app.workers.tasks.scanners.providers import google as _provider_14
from app.workers.tasks.scanners.providers import netlas as _provider_15
from app.workers.tasks.scanners.providers import wayback as _provider_16
from app.workers.tasks.scanners.providers import telegram_search as _provider_17
from app.workers.tasks.scanners.providers import commoncrawl as _provider_18
from app.workers.tasks.scanners.providers import sourcegraph as _provider_19
from app.workers.tasks.scanners.providers import replit as _provider_20
from app.workers.tasks.scanners.providers import postman as _provider_21
from app.workers.tasks.scanners.providers import dockerhub as _provider_22
from app.workers.tasks.scanners.providers import searchcode as _provider_23
from app.workers.tasks.scanners.providers import npm as _provider_24
from app.workers.tasks.scanners.providers import pypi as _provider_25
from app.workers.tasks.scanners.providers import ci_logs as _provider_26

_IMPLEMENTATIONS = (_common,
    _provider_0,
    _provider_1,
    _provider_2,
    _provider_3,
    _provider_4,
    _provider_5,
    _provider_6,
    _provider_7,
    _provider_8,
    _provider_9,
    _provider_10,
    _provider_11,
    _provider_12,
    _provider_13,
    _provider_14,
    _provider_15,
    _provider_16,
    _provider_17,
    _provider_18,
    _provider_19,
    _provider_20,
    _provider_21,
    _provider_22,
    _provider_23,
    _provider_24,
    _provider_25,
    _provider_26)
for _implementation in _IMPLEMENTATIONS:
    for _name, _value in vars(_implementation).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

class _ScannerTaskFacade(_ModuleType):
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

_sys.modules[__name__].__class__ = _ScannerTaskFacade
