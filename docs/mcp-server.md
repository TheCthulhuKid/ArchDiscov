# MCP server

The MCP server serves the [assistant tools](assistant-tools.md) to an MCP client, such as
Claude Code or Claude Desktop, so that the client's own model can answer questions about
selected snapshots with the same bounded, read-only tools the [`ask` command](ask-command.md)
offers. It needs no provider configuration, credentials, or network. It is built in phase 5
of the plan: this page describes what is implemented so far, the server in
`src/archdiscov/assistant/mcp_server.py`, which lists and runs the tools within bounds.
Submitting answers and the `archdiscov mcp` command follow in later steps.

## What the server serves

`build_server(session)` builds a low-level `mcp` server over a `ToolSession` of selected,
verified snapshots. Clients see:

- the server name `archdiscov` and the installed package version;
- the MCP instructions (`MCP_INSTRUCTIONS` in `assistant/instructions.py`): the same rules as
  `ask` runs on evidence, untrusted data, limitations, and snapshot context, but saying that
  the tools reach only the selected snapshots and that every conclusion is to be submitted
  with `submit_answer`, and presented as returned, rather than answered in plain text;
- one tool per tool in the [tool set](assistant-tools.md#tool-set), in the same order, with
  its name, description, and input schema unchanged and no output schema: `search_resources`,
  `inspect_resource`, `get_neighbours`, `find_paths`, `inspect_coverage`, `get_evidence`,
  `list_analyzers`, `run_analyzer`, `list_saved_results`, `get_saved_analysis`, and
  `get_saved_comparison`. `submit_answer` is not served yet.

Only this module imports `mcp`, and nothing imports this module except to build a server:
the other commands, `ask` runs, and evaluations never load the `mcp` package.

## Calls

A call runs exactly as a tool call in an `ask` run:

- Its arguments get the same bounds: a JSON object of at most 16 KiB. A call that omits its
  arguments, as MCP allows for a tool without inputs, is a call with none.
- It is dispatched through the same registry over the connection's session, and the IDs its
  output returns are recorded for that connection. MCP gives calls no ID, so the server
  numbers them per connection (`mcp-1`, `mcp-2`, ...).
- Its result is the tool's JSON output as one text item, with no structured copy or output
  schema. A [tool error](assistant-tools.md) is a result marked as an error, carrying its
  code and fixed message, never a protocol error, so the client can correct the call:

| Code | When |
|---|---|
| `tool_unknown` | no tool has that name, including a name no tool name could take |
| `tool_malformed_arguments` | the arguments exceed 16 KiB |
| `tool_invalid_arguments` | the arguments do not match the tool's input schema |
| `tool_not_found`, `tool_unavailable` | as the tool reports them |
| `tool_output_too_large` | the output exceeds the session's output limit |
| `tool_internal_error` | anything else failed; no exception text or traceback reaches the client |
| `tool_session_limit_reached` | the connection has used its call cap (see [limits](#limits)); nothing is dispatched |

Calls on one connection run one at a time, in the order they arrive, even when a client
sends several at once, so the run record sees them in order.

## Limits

Each call has the session's per-call [tool limits](assistant-tools.md#limits-and-truncation), as in an
`ask` run: by default 25 results, a depth of 3, and 32,768 characters of output, with
explicit truncation.

Each connection also has a cap on its tool calls, `max_tool_calls` in `ServerLimits`:
200 by default, from 1 to 2,000. Every call counts, errors included. Past the cap, every
call returns `tool_session_limit_reached` and dispatches nothing, so nothing more is
recorded; a new connection starts with its full count. The cap is a backstop against a
runaway client, not a budget: answering every README example question in one connection
takes a few dozen calls.

## Connections

The server keeps no state of its own. Each connection gets its own fresh copy of the
selected session: the verified snapshot data is shared, while the run record of IDs the
tools returned starts empty and belongs to that connection, with its own call count. One
connection never sees the IDs another connection's tools returned, and the server does not
depend on the transport that serves it.
