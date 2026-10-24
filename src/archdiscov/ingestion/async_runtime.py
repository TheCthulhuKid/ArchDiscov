"""Bridge blocking work without mistaking await cancellation for thread completion."""

import asyncio
from collections.abc import Awaitable, Callable

from ..core.async_bridge import run_sync as core_run_sync
from ..core.async_bridge import wait_until_done
from .adapter import IngestionError


async def await_settled[T](
    awaitable: Awaitable[T], *, on_cancel: Callable[[], None] | None = None
) -> T:
    future = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        # Repeated cancellation must not release ownership of a still-running thread.
        await wait_until_done(future)
        raise


def run_sync[T](workflow: Callable[[], Awaitable[T]]) -> T:
    return core_run_sync(
        workflow,
        lambda: IngestionError(
            "invalid_request", "use the async collection API inside a running event loop"
        ),
    )
