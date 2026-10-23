"""Serve selected snapshots to an MCP client over stdio.

The selection is loaded and verified first, exactly as `ask` loads it, so a missing or
invalid snapshot or a saved comparison of other snapshots fails with the same errors before
anything is served, and before `mcp` is imported. Serving then runs until the client closes
the connection. `mcp`'s loggers are held at `WARNING`, since its debug records hold the
messages it sends and receives.

Standard input is read by a daemon thread of this module rather than by `stdio_server`'s
own reader, whose worker thread cannot be abandoned: with it, an interrupt would wait for
the client's next line before the server could stop.
"""

import asyncio
import os
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from archdiscov.assistant.adapters import quiet_loggers
from archdiscov.assistant.mcp_limits import ServerLimits
from archdiscov.assistant.tools.session import ToolSession

_MCP_LOGGERS = ("mcp",)
_READ_BYTES = 65_536


class _StdinLines:
    """Standard input's lines, decoded as `stdio_server` decodes them, for its `async for`.

    A daemon thread reads and queues each line, so a cancelled read stops waiting at once and
    the thread, still blocked on input, ends with the process. It reads a duplicate of the
    descriptor without a Python file object, whose lock a blocked read would hold while the
    interpreter shuts down.
    """

    def __init__(self) -> None:
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        threading.Thread(target=self._read, args=(loop,), daemon=True).start()

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            for line in _lines(os.dup(sys.stdin.fileno())):
                loop.call_soon_threadsafe(self._lines.put_nowait, line)
            loop.call_soon_threadsafe(self._lines.put_nowait, None)
        except RuntimeError:
            # The loop closed after an interrupt; nothing is waiting for input any more.
            return

    def __aiter__(self) -> "_StdinLines":
        return self

    async def __anext__(self) -> str:
        line = await self._lines.get()
        if line is None:
            raise StopAsyncIteration
        return line


def _lines(descriptor: int) -> Iterator[str]:
    """The descriptor's lines until end of input, each with its newline; closes it at the end."""
    pending = b""
    try:
        while chunk := os.read(descriptor, _READ_BYTES):
            *complete, pending = (pending + chunk).split(b"\n")
            yield from (line.decode("utf-8", errors="replace") + "\n" for line in complete)
        if pending:
            yield pending.decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)


# A transport opens the server's read and write streams; the default is stdio.
type Transport = Callable[[], AbstractAsyncContextManager[tuple[Any, Any]]]


def serve_selection(
    snapshot_path: Path,
    *,
    comparison_path: Path | None = None,
    comparison_result_paths: Sequence[Path] = (),
    limits: ServerLimits | None = None,
) -> None:
    """Verify the selected files, then serve them over stdio until the client disconnects.

    Raises the selection errors `ask` raises before anything is served; an interrupt
    (`KeyboardInterrupt`) reaches the caller once the standard streams are restored.
    """
    session = ToolSession.from_paths(
        snapshot_path,
        comparison_path=comparison_path,
        comparison_result_paths=comparison_result_paths,
    )
    asyncio.run(serve_session(session, limits))


async def serve_session(
    session: ToolSession, limits: ServerLimits | None = None, *, transport: Transport | None = None
) -> None:
    """Serve a verified session over `transport` (stdio by default) until it closes."""
    from mcp.server.stdio import stdio_server

    from archdiscov.assistant.mcp_server import build_server

    server = build_server(session, limits)
    quiet_loggers(_MCP_LOGGERS)
    # Standard output is still claimed and diverted by `stdio_server`; only input is ours.
    stdio = transport or (lambda: stdio_server(stdin=_StdinLines()))
    async with stdio() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
