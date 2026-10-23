"""Serve selected snapshots to an MCP client over stdio.

The selection is loaded and verified first, exactly as `ask` loads it, so a missing or
invalid snapshot or a saved comparison of other snapshots fails with the same errors before
anything is served, and before `mcp` is imported. Serving then runs until the client closes
the connection. `mcp`'s loggers are held at `WARNING`, since its debug records hold the
messages it sends and receives.
"""

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from archdiscov.assistant.adapters import quiet_loggers
from archdiscov.assistant.mcp_limits import ServerLimits
from archdiscov.assistant.tools.session import ToolSession

_MCP_LOGGERS = ("mcp",)

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
    async with (transport or stdio_server)() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
