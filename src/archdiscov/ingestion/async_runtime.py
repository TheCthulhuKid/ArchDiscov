"""Bridge blocking work without mistaking await cancellation for thread completion."""

import asyncio
from collections.abc import Awaitable, Callable

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
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not future.cancelled():
            future.exception()
        raise


def run_sync[T](workflow: Callable[[], Awaitable[T]]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(workflow())
    raise IngestionError(
        "invalid_request", "use the async collection API inside a running event loop"
    )
