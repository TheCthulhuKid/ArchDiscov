"""An MCP server over the assistant tools, for an MCP client to be the model.

The server lists the same tools `ask` offers, from the default registry, with their names,
descriptions, and input schemas unchanged, and gives clients the MCP instructions. A call
is bounded and dispatched as `ask` dispatches one, and its result is the tool's JSON as
text, a tool error included, so a client can recover from it. Each connection works on its
own fresh copy of the selected session: the verified snapshot data is shared, the run
record of returned IDs is not, so one connection never sees another's returned IDs. The
server keeps no state outside a connection, so it does not depend on the transport that
serves it.

This is the only module that imports `mcp`; import it only to build a server.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version

from mcp import types
from mcp.server import Server, ServerRequestContext
from pydantic import ValidationError

from .conversation import ToolCall, ToolResult, parse_tool_arguments
from .instructions import MCP_INSTRUCTIONS
from .tools.catalog import default_registry
from .tools.contracts import ToolErrorCode, render, tool_error
from .tools.registry import ToolRegistry
from .tools.session import ToolSession

SERVER_NAME = "archdiscov"


@dataclass
class Connection:
    """What one client connection holds: its session copy and how many calls it made, which
    also numbers its call IDs (MCP gives none)."""

    session: ToolSession
    calls: int = 0

    def next_call_id(self) -> str:
        self.calls += 1
        return f"mcp-{self.calls}"


def _tools(registry: ToolRegistry) -> list[types.Tool]:
    return [
        types.Tool(
            name=declaration.name,
            description=declaration.description,
            input_schema=dict(declaration.input_schema),
        )
        for declaration in registry.declarations()
    ]


def _error(call_id: str, code: ToolErrorCode, connection: Connection) -> ToolResult:
    result, _ = render(call_id, tool_error(code), connection.session.limits)
    return result


def _dispatch(
    registry: ToolRegistry, connection: Connection, params: types.CallToolRequestParams
) -> ToolResult:
    call_id = connection.next_call_id()
    # MCP lets a call without inputs omit its arguments.
    arguments = parse_tool_arguments({} if params.arguments is None else params.arguments)
    try:
        call = ToolCall(id=call_id, name=params.name, arguments=arguments)
    except ValidationError:
        # Only the client's name can fail: one outside the tool-name pattern names no tool.
        return _error(call_id, ToolErrorCode.UNKNOWN_TOOL, connection)
    return registry.dispatch(connection.session, call)


def build_server(session: ToolSession) -> Server[Connection]:
    """A server over `session`'s verified data; each connection gets a fresh run record."""
    registry = default_registry()
    tools = _tools(registry)

    @asynccontextmanager
    async def connection(_: Server[Connection]) -> AsyncIterator[Connection]:
        yield Connection(session.fresh())

    async def list_tools(
        _: ServerRequestContext[Connection], __: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(
        context: ServerRequestContext[Connection], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        state = context.lifespan_context
        # Dispatch runs here, without an await, so calls on a connection never interleave
        # and the run record sees them in the order they arrived.
        try:
            result = _dispatch(registry, state, params)
        except Exception:
            result = _error(f"mcp-{state.calls}", ToolErrorCode.INTERNAL_ERROR, state)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.content)],
            is_error=result.is_error,
        )

    return Server(
        SERVER_NAME,
        version=version("archdiscov"),
        instructions=MCP_INSTRUCTIONS,
        lifespan=connection,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
