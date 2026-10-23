"""Bounds for an MCP server connection, apart from the server so that a command can read and
validate them without importing `mcp`."""

from pydantic import Field

from .conversation import Contract


class ServerLimits(Contract):
    """Bounds for one connection. The tool-call cap is a backstop against a runaway client,
    not a budget: answering every README example in one connection takes a few dozen calls,
    and a client that keeps a connection for several questions stays well below it."""

    max_tool_calls: int = Field(default=200, ge=1, le=2_000, strict=True)
