# MCP server

The MCP server serves the [assistant tools](assistant-tools.md) to an MCP client, such as
Claude Code or Claude Desktop, so that the client's own model can answer questions about
selected snapshots with the same bounded, read-only tools the [`ask` command](ask-command.md)
offers. It needs no provider configuration, credentials, or network. It is built in phase 5
of the plan, and this page describes what is implemented so far:

- the server in `src/archdiscov/assistant/mcp_server.py`, which lists and runs the tools
  within bounds, validates submitted answers, and offers the README questions as prompts;
- the service in `src/archdiscov/services/mcp.py`, which serves a verified selection over
  stdio;
- the `archdiscov mcp` command in `src/archdiscov/cli_mcp.py`, which an MCP client starts.

## Running the server

```sh
archdiscov mcp --snapshot PATH [--compare PATH] [--comparison-result PATH ...] [--max-tool-calls N]
```

| Option | Meaning |
|---|---|
| `--snapshot` | required: the snapshot JSON to serve |
| `--compare` | a second snapshot, for comparison questions, as in `ask` |
| `--comparison-result` | a saved comparison of the selected snapshots for the client to read; repeatable |
| `--max-tool-calls` | the [call cap](#limits) for one client connection, submissions included |

The command verifies the selected files as [`ask`](ask-command.md) does, then serves them
over stdio to the client that started it. The snapshots are chosen when the server starts
and cannot change during a session: no tool opens another path, writes a file, or reaches a
network. Standard output carries only protocol messages, so there is no `--json`;
diagnostics before serving are readable, on standard error. The client also sees the
README questions as [prompts](#prompts).

| Exit code | When |
|---|---|
| 0 | the client disconnected |
| 1 | a selected file could not be read (`snapshot_io_error`) |
| 2 | invalid input before serving: `invalid_snapshot`, `invalid_comparison_result`, `result_snapshot_unselected`, or a usage error such as a `--max-tool-calls` outside its range |
| 130 | the server was interrupted, for example with Ctrl-C |

## Client setup

Clients start the server themselves, so give them absolute paths, chosen when you configure
the client. Replace the paths below with your own; the server needs no credentials.

**Claude Code.** Add the server with `claude mcp add`; everything after `--` is the command
Claude Code runs:

```sh
claude mcp add --transport stdio archdiscov -- archdiscov mcp --snapshot /absolute/path/snapshot.json
```

The server is then available in the current project (`--scope user` makes it available in
every project). `claude mcp list` shows it, and `/mcp` inside Claude Code shows its status.
The prompts appear as commands, such as `/archdiscov:ownership`. If `archdiscov` is not on
the `PATH` Claude Code starts with, use the executable's absolute path, for example
`/absolute/path/.venv/bin/archdiscov`.

**Claude Desktop.** Add an entry under `mcpServers` in `claude_desktop_config.json`
(Settings, Developer, Edit Config; on macOS it is in
`~/Library/Application Support/Claude/`, on Windows in `%APPDATA%\Claude\`), then quit and
restart Claude Desktop:

```json
{
  "mcpServers": {
    "archdiscov": {
      "command": "/absolute/path/.venv/bin/archdiscov",
      "args": ["mcp", "--snapshot", "/absolute/path/snapshot.json"]
    }
  }
}
```

Claude Desktop does not start the server from a shell, so `command` is the executable's
absolute path. Its log of the server's standard error is `mcp-server-archdiscov.log`, in
`~/Library/Logs/Claude` on macOS or `%APPDATA%\Claude\logs` on Windows. Both setups were
checked against the clients' documentation (`https://code.claude.com/docs/en/mcp` and
`https://modelcontextprotocol.io/docs/develop/connect-local-servers`); they cannot be
exercised offline.

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
  `get_saved_comparison`;
- then [`submit_answer`](#answers), with the same proposal schema as in `ask` and a
  description for MCP: submit before presenting any conclusion, correct and resubmit a
  rejected answer, and present an accepted one as returned;
- one [prompt](#prompts) per README example question.

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

Each connection also has a cap on its tool calls, `max_tool_calls` in `ServerLimits`
(`assistant/mcp_limits.py`, which does not import `mcp`, so a command can read the bounds):
200 by default, from 1 to 2,000. Every call counts, errors included. Past the cap, every
call returns `tool_session_limit_reached` and dispatches nothing, so nothing more is
recorded; a new connection starts with its full count. The cap is a backstop against a
runaway client, not a budget: answering every README example question in one connection
takes a few dozen calls.

## Answers

`submit_answer` takes the same [proposal](assistant-answers.md#the-proposal) as in `ask`:
status, contexts, cited conclusions, acknowledged limitations, and a short summary and
explanation. Its description shares `ask`'s citation rules but replaces "Submit the final
answer exactly once" with two MCP sentences: "Submit an answer before presenting any
conclusion about the snapshots." and "If it is rejected, correct it and submit it again; if
it is accepted, present the returned answer as it is."

A `submit_answer` call is never dispatched as a tool. It counts toward the call cap like
any call, its arguments get the same bounds, and it is read and
[validated](assistant-answers.md#validation) exactly as an `ask` run validates a proposal,
against every ID this connection's tools have returned so far. The result depends on the
submission:

| Submission | Error | Text items |
|---|---|---|
| unreadable | yes | one JSON object: `error` (`proposal_malformed` or `proposal_invalid`), `message`, `next` |
| rejected | yes | one JSON object: `error` (`answer_rejected`), `rejections` (each with `code`, `location`, and `message`, at most 40), `unlisted`, `next` |
| accepted | no | three: the line "The answer was accepted. Present it to the user as it is returned below, without adding conclusions to it.", the readable answer, and the answer JSON, compact |

The error objects have the same form as `ask`'s repair prompt, with `next` set to "Correct
the answer and submit it again." An accepted answer is rendered as in `ask`, with the
outcome `answered`: the readable answer is exactly what `ask` prints, and the answer JSON is
the same data that `ask --json` reports as its `result`, without indentation, since the
readable answer beside it is the form a person reads. Claude Code moves a tool result over
50,000 characters out of the conversation into a file unless the tool declares a larger
limit, so `submit_answer` declares `"_meta": {"anthropic/maxResultSizeChars": 500000}`,
Claude Code's ceiling, and other clients ignore it; every README example's accepted answer
also fits under 50,000 characters without it. Tests submit every rejection scenario of the
answer suite and the adversarial suite's proposals through the server and compare them with
`validate_proposal`, and render every README example and the injected-text case through
both and compare them, readable and JSON.

The run record is session-wide: a proposal may cite any ID the connection's tools returned,
whichever question they were called for. Each submission is validated on its own, with no
repair budget, so an accepted answer is `answered` however many rejected ones came before
it. Validating and rendering never change the run record.

**What the server cannot enforce.** The client writes its own text to the user. The server
validates and renders only what is submitted, and its instructions and the `submit_answer`
description ask the client to present an accepted answer as returned; nothing stops a client
from stating conclusions it never submitted, or from changing an accepted answer when it
presents it. Treat only the answer the server returned as validated.

## Prompts

The server offers the README's five example questions as prompts, from
`assistant/questions.py`, which the README check also reads. Each prompt is named by the
analyzer kind that answers its question (`internet_exposure`, `database_access`,
`security_group_impact`, `cross_account_trust`, and `ownership`, in the README's order),
takes no arguments, and returns one user message: the question exactly as the README states
it, with no instructions of its own. A client can start a supported question from it in one
step; the answer still goes through the tools and `submit_answer`.

An unknown prompt name, or arguments for a prompt, is an MCP invalid-params error with the
fixed message "No prompt with that name takes these arguments.", which never echoes the
request.

## Serving

`serve_selection(snapshot_path, comparison_path=None, comparison_result_paths=(),
limits=None)` loads and verifies the selected files as `ask` does, so a missing or invalid
snapshot, or a saved comparison of snapshots that were not selected, fails with the same
errors before anything is served and before `mcp` is imported. It then serves the selection
over stdio until the client closes the connection, and returns. `mcp`'s `stdio_server`
points the process's standard output at standard error while serving, so stray output
cannot reach the protocol stream, and `mcp`'s loggers are held at `WARNING`, since their
debug records hold the messages sent and received. Standard input is read by the service's
own daemon thread rather than by `stdio_server`'s reader, whose worker thread cannot be
abandoned, so an interrupt while the server waits for the client's next line stops it at
once instead of waiting for that line; it reaches the caller once the standard streams are
restored. A client that closes its side ends the session, and requests still in flight are
cancelled, so a client waits for its answers before disconnecting. `serve_session` serves an already verified session over any
transport, which tests use with in-memory streams.

## Connections

The server keeps no state of its own. Each connection gets its own fresh copy of the
selected session: the verified snapshot data is shared, while the run record of IDs the
tools returned starts empty and belongs to that connection, with its own call count. One
connection never sees the IDs another connection's tools returned, and the server does not
depend on the transport that serves it.
