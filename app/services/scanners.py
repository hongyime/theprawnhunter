"""Compatibility re-export facade for primary scanner providers."""
from types import ModuleType as _ModuleType
import sys as _sys

from app.infrastructure.scanners import common as _common
from app.infrastructure.scanners.providers import shodan as _provider_0
from app.infrastructure.scanners.providers import fofa as _provider_1
from app.infrastructure.scanners.providers import urlscan as _provider_2
from app.infrastructure.scanners.providers import github as _provider_3
from app.infrastructure.scanners.providers import gitlab as _provider_4
from app.infrastructure.scanners.providers import exa as _provider_5
from app.infrastructure.scanners.providers import wayback as _provider_6
from app.infrastructure.scanners.providers import commoncrawl as _provider_7
from app.infrastructure.scanners.providers import sourcegraph as _provider_8

_IMPLEMENTATIONS = (_common,
    _provider_0,
    _provider_1,
    _provider_2,
    _provider_3,
    _provider_4,
    _provider_5,
    _provider_6,
    _provider_7,
    _provider_8)
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
