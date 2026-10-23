"""Bounds for an MCP server connection, apart from the server so that a command can read and
validate them without importing `mcp`."""

from pydantic import Field

from .conversation import Contract

DEFAULT_MAX_TOOL_CALLS = 200
MIN_MAX_TOOL_CALLS, MAX_MAX_TOOL_CALLS = 1, 2_000


class ServerLimits(Contract):
    """Bounds for one connection. The tool-call cap is a backstop against a runaway client,
    not a budget: answering every README example in one connection takes a few dozen calls,
    and a client that keeps a connection for several questions stays well below it."""

    max_tool_calls: int = Field(
        default=DEFAULT_MAX_TOOL_CALLS, ge=MIN_MAX_TOOL_CALLS, le=MAX_MAX_TOOL_CALLS, strict=True
    )
