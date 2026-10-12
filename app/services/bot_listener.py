"""Compatibility shim for the bot listener service and CLI entrypoint."""
from types import ModuleType as _ModuleType
import os
import sys

# Keep the project-root path behavior of the historical module location.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.services._bot_listener import common as _common
from app.services._bot_listener.common import *
from app.services._bot_listener import commands as _commands
from app.services._bot_listener import login as _login
from app.services._bot_listener import updates as _updates
from app.services._bot_listener import runtime as _runtime

_IMPLEMENTATIONS = (_common, _commands, _login, _updates, _runtime)
for _implementation in _IMPLEMENTATIONS:
    for _name, _value in vars(_implementation).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

class _BotListenerFacade(_ModuleType):
    def __getattribute__(self, name):
        if name == "redis_client":
            runtime = super().__getattribute__("_runtime")
            return runtime.redis_client
        return super().__getattribute__(name)

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

sys.modules[__name__].__class__ = _BotListenerFacade

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Fatal crash: {e}")
