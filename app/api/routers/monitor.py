"""Compatibility facade for the resource-based monitor API routers."""
from types import ModuleType as _ModuleType
import sys as _sys

from app.api.routers._monitor import common as _common
from app.api.routers._monitor.common import *
from app.api.routers._monitor import stats as _stats
from app.api.routers._monitor import credentials as _credentials
from app.api.routers._monitor import messages as _messages
from app.api.routers._monitor import findings as _findings
from app.api.routers._monitor import engagement as _engagement
from app.api.routers._monitor import exports as _exports
from app.api.routers._monitor import broadcasts as _broadcasts
from app.api.routers._monitor import topics as _topics
from app.api.routers._monitor import webhooks as _webhooks
from app.api.routers._monitor import targets as _targets
from app.api.routers._monitor import search as _search
from app.api.routers._monitor import operators as _operators

router = APIRouter(
    prefix="/monitor",
    tags=["Monitor"],
    dependencies=[Depends(require_monitor_key)],
)
router.include_router(_stats.router)
router.include_router(_credentials.router)
router.include_router(_messages.router)
router.include_router(_findings.router)
router.include_router(_engagement.router)
router.include_router(_exports.router)
router.include_router(_broadcasts.router)
router.include_router(_topics.router)
router.include_router(_webhooks.router)
router.include_router(_targets.router)
router.include_router(_search.router)
router.include_router(_operators.router)

_IMPLEMENTATIONS = (_common,
    _stats,
    _credentials,
    _messages,
    _findings,
    _engagement,
    _exports,
    _broadcasts,
    _topics,
    _webhooks,
    _targets,
    _search,
    _operators)
for _implementation in _IMPLEMENTATIONS:
    for _name, _value in vars(_implementation).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

class _MonitorFacade(_ModuleType):
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

_sys.modules[__name__].__class__ = _MonitorFacade
