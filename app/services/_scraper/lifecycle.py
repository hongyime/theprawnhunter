import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any


class TelegramClientLifecycle:
    """
    Small owner for Telegram async operations that must time out cleanly and
    disconnect even when a task is cancelled or the network drops.
    """

    def __init__(
        self,
        *,
        client: Any = None,
        connect: Callable[[], Awaitable[Any]] | None = None,
        disconnect: Callable[[], Awaitable[Any]] | None = None,
        timeout: float | None = None,
        disconnect_timeout: float = 10.0,
        label: str = "telegram_client",
        logger: logging.Logger | None = None,
    ):
        self.client = client
        self.connect = connect
        self.disconnect = disconnect
        self.timeout = timeout
        self.disconnect_timeout = disconnect_timeout
        self.label = label
        self.logger = logger or logging.getLogger(__name__)
        self._tasks: set[asyncio.Task] = set()

    async def __aenter__(self):
        if self.connect:
            connected = await self._await_with_timeout(
                self.connect(),
                timeout=self.timeout,
                label=f"{self.label}.connect",
            )
            if connected is not None:
                self.client = connected
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        await self.cancel_pending()
        await self.disconnect_safely()
        return False

    async def run(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        timeout: float | None = None,
        label: str | None = None,
    ) -> Any:
        task = asyncio.create_task(operation())
        self._tasks.add(task)
        try:
            return await asyncio.wait_for(task, timeout=timeout if timeout is not None else self.timeout)
        except Exception:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise
        finally:
            self._tasks.discard(task)

    async def cancel_pending(self) -> None:
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.difference_update(pending)

    async def disconnect_safely(self) -> None:
        if not self.disconnect:
            return
        try:
            await asyncio.wait_for(self.disconnect(), timeout=self.disconnect_timeout)
        except TimeoutError:
            self.logger.warning("%s disconnect timed out", self.label)
        except Exception as exc:
            self.logger.warning("%s disconnect failed: %s", self.label, exc)

    @staticmethod
    async def _await_with_timeout(
        awaitable: Awaitable[Any],
        *,
        timeout: float | None,
        label: str,
    ) -> Any:
        if timeout is None:
            return await awaitable
        task = asyncio.create_task(awaitable)
        try:
            return await asyncio.wait_for(task, timeout=timeout)
        except Exception:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise

    @staticmethod
    async def run_with_disconnect(
        operation: Callable[[], Awaitable[Any]],
        *,
        disconnect: Callable[[], Awaitable[Any]],
        timeout: float | None = None,
        disconnect_timeout: float = 10.0,
        label: str = "telegram_client",
        logger: logging.Logger | None = None,
    ) -> Any:
        lifecycle = TelegramClientLifecycle(
            disconnect=disconnect,
            timeout=timeout,
            disconnect_timeout=disconnect_timeout,
            label=label,
            logger=logger,
        )
        async with lifecycle:
            return await lifecycle.run(operation, timeout=timeout, label=label)
