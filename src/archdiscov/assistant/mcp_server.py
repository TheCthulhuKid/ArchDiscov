"""An MCP server over the assistant tools, for an MCP client to be the model.

The server lists the same tools `ask` offers, from the default registry, with their names,
descriptions, and input schemas unchanged, and gives clients the MCP instructions. Each
connection works on its own fresh copy of the selected session: the verified snapshot data
is shared, the run record of returned IDs is not, so one connection never sees another's
returned IDs. The server keeps no state outside a connection, so it does not depend on the
transport that serves it.

This is the only module that imports `mcp`; import it only to build a server.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version

from mcp import types
from mcp.server import Server, ServerRequestContext

from .instructions import MCP_INSTRUCTIONS
from .tools.catalog import default_registry
from .tools.session import ToolSession

SERVER_NAME = "archdiscov"


@dataclass
class Connection:
    """What one client connection holds: its session copy and how many calls it made."""

    session: ToolSession
    calls: int = 0


def _tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=declaration.name,
            description=declaration.description,
            input_schema=dict(declaration.input_schema),
        )
        for declaration in default_registry().declarations()
    ]


def build_server(session: ToolSession) -> Server[Connection]:
    """A server over `session`'s verified data; each connection gets a fresh run record."""
    tools = _tools()

    @asynccontextmanager
    async def connection(_: Server[Connection]) -> AsyncIterator[Connection]:
        yield Connection(session.fresh())

    async def list_tools(
        _: ServerRequestContext[Connection], __: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    return Server(
        SERVER_NAME,
        version=version("archdiscov"),
        instructions=MCP_INSTRUCTIONS,
        lifespan=connection,
        on_list_tools=list_tools,
    )
