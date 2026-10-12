"""Compatibility facade for the Telegram user-agent service."""
from types import ModuleType as _ModuleType
import sys as _sys

from app.services._user_agent import common as _common
from app.services._user_agent.common import *
from app.services._user_agent import session_lifecycle as _session_lifecycle
from app.services._user_agent import group_archive as _group_archive

class UserAgentService(_session_lifecycle.UserAgentSessionLifecycle, _group_archive.UserAgentGroupArchive):
    'Service acting as a real Telegram User (not a bot).\nUsed for actions bots cannot perform, like inviting other bots to groups.'

user_agent = UserAgentService()

_IMPLEMENTATIONS = (_common, _session_lifecycle, _group_archive)
for _implementation in _IMPLEMENTATIONS:
    for _name, _value in vars(_implementation).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

class _UserAgentFacade(_ModuleType):
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

_sys.modules[__name__].__class__ = _UserAgentFacade
