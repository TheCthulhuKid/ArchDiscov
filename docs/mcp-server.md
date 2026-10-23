# MCP server

The MCP server serves the [assistant tools](assistant-tools.md) to an MCP client, such as
Claude Code or Claude Desktop, so that the client's own model can answer questions about
selected snapshots with the same bounded, read-only tools the [`ask` command](ask-command.md)
offers. It needs no provider configuration, credentials, or network. It is built in phase 5
of the plan: this page describes what is implemented so far, the server in
`src/archdiscov/assistant/mcp_server.py`. Calling tools, submitting answers, limits, and the
`archdiscov mcp` command follow in later steps.

## What the server serves

`build_server(session)` builds a low-level `mcp` server over a `ToolSession` of selected,
verified snapshots. Clients see:

- the server name `archdiscov` and the installed package version;
- the MCP instructions (`MCP_INSTRUCTIONS` in `assistant/instructions.py`): the same rules as
  `ask` runs on evidence, untrusted data, limitations, and snapshot context, but saying that
  the tools reach only the selected snapshots and that every conclusion is to be submitted
  with `submit_answer`, and presented as returned, rather than answered in plain text;
- one tool per tool in the [tool set](assistant-tools.md#tool-set), in the same order, with
  its name, description, and input schema unchanged and no output schema. `submit_answer`
  is not served yet.

Only this module imports `mcp`, and nothing imports this module except to build a server:
the other commands, `ask` runs, and evaluations never load the `mcp` package.

## Connections

The server keeps no state of its own. Each connection gets its own fresh copy of the
selected session: the verified snapshot data is shared, while the run record of IDs the
tools returned starts empty and belongs to that connection, with its own call count. One
connection never sees the IDs another connection's tools returned, and the server does not
depend on the transport that serves it.
