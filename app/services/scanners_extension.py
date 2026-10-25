"""Compatibility re-export facade for extension scanner providers."""
from types import ModuleType as _ModuleType
import sys as _sys

from app.infrastructure.scanners import common as _common
from app.infrastructure.scanners.providers import github_gist as _provider_0
from app.infrastructure.scanners.providers import grepapp as _provider_1
from app.infrastructure.scanners.providers import publicwww as _provider_2
from app.infrastructure.scanners.providers import google as _provider_3
from app.infrastructure.scanners.providers import bitbucket as _provider_4
from app.infrastructure.scanners.providers import pastebin as _provider_5
from app.infrastructure.scanners.providers import rentry as _provider_6
from app.infrastructure.scanners.providers import hastebin as _provider_7
from app.infrastructure.scanners.providers import netlas as _provider_8
from app.infrastructure.scanners.providers import replit as _provider_9
from app.infrastructure.scanners.providers import postman as _provider_10
from app.infrastructure.scanners.providers import searchcode as _provider_11
from app.infrastructure.scanners.providers import npm as _provider_12
from app.infrastructure.scanners.providers import pypi as _provider_13
from app.infrastructure.scanners.providers import ci_logs as _provider_14

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
    _provider_14)
for _implementation in _IMPLEMENTATIONS:
    for _name, _value in vars(_implementation).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

class _ScannerFacade(_ModuleType):
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

_sys.modules[__name__].__class__ = _ScannerFacade
