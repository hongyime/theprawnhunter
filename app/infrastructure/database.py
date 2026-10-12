"""Shared async boundary for synchronous Supabase query execution."""

import asyncio
from typing import Any


async def execute_async(query_builder: Any) -> Any:
    """Execute a synchronous Supabase query builder outside the event loop."""
    return await asyncio.to_thread(query_builder.execute)


__all__ = ["execute_async"]
