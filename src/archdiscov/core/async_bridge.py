"""Running async workflows from synchronous code, and settling cancelled work.

A cancelled future that wraps a thread or provider call may still be running: waiting until
it has really finished keeps its owner from releasing what it still uses, so cancellation
is never mistaken for completion.
"""

import asyncio
import contextvars
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future


def run_sync[T](workflow: Callable[[], Awaitable[T]], in_loop: Callable[[], Exception]) -> T:
    """Run `workflow` to completion; raise `in_loop()` inside a running event loop, where
    the caller must await the async API instead."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(workflow())
    raise in_loop()


async def wait_until_done(future: asyncio.Future) -> None:
    """Wait until `future` has finished, through repeated cancellation, and consume its
    exception so none is reported as never retrieved."""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001 - the future finished; its exception is consumed
            break
    if not future.cancelled():
        future.exception()


async def in_daemon_thread[T](function: Callable[[], T]) -> T:
    """Run blocking `function` in its own daemon thread, as `asyncio.to_thread` would.

    `asyncio.run` joins the default executor's threads before it returns, so an interrupt
    during a blocking network call would wait out that call's timeout. A daemon thread that
    is still blocked when the workflow is cancelled ends with the process instead.
    """
    done: Future[T] = Future()

    def run() -> None:
        if not done.set_running_or_notify_cancel():
            return
        try:
            done.set_result(function())
        except BaseException as error:  # noqa: BLE001 - handed to the awaiting coroutine
            done.set_exception(error)

    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(run,), daemon=True).start()
    return await asyncio.wrap_future(done)
