import asyncio
import signal
import subprocess
import sys
import threading
import time

import pytest

from archdiscov.core.async_bridge import in_daemon_thread


def test_a_daemon_thread_call_returns_its_result_and_raises_its_error():
    def fail():
        raise ValueError("boom")

    assert asyncio.run(in_daemon_thread(lambda: threading.current_thread().daemon))
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(in_daemon_thread(fail))


def test_a_cancelled_daemon_thread_call_does_not_hold_up_the_loop():
    release = threading.Event()

    async def cancelled():
        task = asyncio.ensure_future(in_daemon_thread(lambda: release.wait(5)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    start = time.monotonic()
    asyncio.run(cancelled())
    release.set()
    assert time.monotonic() - start < 2


# `asyncio.to_thread` here would keep the process alive for the whole blocking call.
_INTERRUPTED = """
import asyncio, sys, time
from archdiscov.core.async_bridge import in_daemon_thread

async def main():
    await in_daemon_thread(lambda: time.sleep(30))

print("ready", flush=True)
try:
    asyncio.run(main())
except KeyboardInterrupt:
    sys.exit(130)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="sends SIGINT")
def test_an_interrupt_does_not_wait_for_a_blocked_daemon_thread_call():
    process = subprocess.Popen(
        [sys.executable, "-c", _INTERRUPTED], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout.readline() == "ready\n"
    time.sleep(0.2)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=10) == 130
