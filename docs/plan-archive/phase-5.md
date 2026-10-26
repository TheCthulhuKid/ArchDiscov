# Phase 5: MCP server over the assistant tools

Archived from [PLAN.md](../../PLAN.md). Steps 45–48 are complete; this file records
how they were planned and verified and is no longer updated.

**Phase 5 milestone:** An MCP client (for example Claude Code or Claude Desktop) starts `archdiscov mcp` over stdio for selected snapshots → calls the same bounded, read-only tools the `ask` command offers → submits an answer that is validated and rendered from tool-returned records exactly as `ask` validates and renders it.

The server lets a user's own MCP client be the model, so it needs no provider configuration, credentials, or network. It reuses the phase 4 tool registry, `ToolSession`, run record, answer validation, and rendering unchanged, and adds no analyzer, collector, or cloud platform; AWS stays the primary platform. The server is local and read-only: stdio transport only, snapshots selected when it starts, no tool that opens files, changes data, or reaches a network. Its limit is honest: an MCP client writes its own text to the user, so the server can validate and render what the client submits, and mark everything it returns as data, but cannot stop a client from stating unvalidated claims outside a submitted answer. The documentation says so.

Implement steps 45–48 in order, verifying each before the next. Step 48 passed, so this phase is complete. The phase 4 open question on the `mcp` dependency is settled: it stays, as this phase's server.

**Where things go:**
- The server lives in `src/archdiscov/assistant/mcp_server.py`, the only module that imports `mcp`, and only when the server is built.
- The command lives in `cli_mcp.py`, which stays routing and presentation.

**What is reused:**
- `default_registry()`, its declarations, and `ToolRegistry.dispatch`, so a tool behaves identically over MCP and in `ask`.
- `ToolSession.from_paths` and the `ask` pre-run diagnostics and exit codes for the selected files.
- `validate_proposal`, `render_answer`, and the `cli_answer_output` renderings for submitted answers.
- The assistant instructions, adapted as the server's instructions.
- The installed-CLI harness and offline guard for acceptance.

## Step 45: Serve the tool registry over MCP
**Status:** Implemented. Substeps 45.1–45.3 pass their acceptance checks: `assistant/mcp_server.py` serves the registry's tools unchanged over a low-level `mcp` server, dispatches calls as `ask` does with per-connection run records and ordered calls, and bounds each connection with a call cap.
**Prerequisite:** Completed phase 4.

- Build an MCP server from a `ToolSession` and the default registry: one MCP tool per declaration (name, description, and input schema unchanged), each call parsed as `ask` parses arguments and dispatched through `ToolRegistry.dispatch`. A result is the tool's JSON output as text content (and structured content where the client supports it); a tool error is an MCP error result with the same `ToolError` code and fixed message, never a protocol failure.
- Give the server instructions adapted from the assistant instructions: records are data, imported text is quoted and never an instruction, and conclusions must be submitted for validation (step 46).
- Bound a server session as a run is bounded: the existing `ToolLimits` per call, plus a session-wide cap on tool calls (default stated in the docs), after which tools return a limit error.
- Import `mcp` only when the server is built; extend the import-isolation test so `ask`, `evaluate`, and the other commands never import it.
- Keep the server independent of its transport: build it from a session and limits, and hold every piece of state (the run record and the call count) per connection, never in module globals. Only step 47 chooses stdio, so a later transport adds a listener, not a second server.

**Advice:** Keep the server a thin adapter over the registry. If an MCP tool needs behaviour the registry lacks, add it to the registry so `ask` gains it too.

**Acceptance check:** In-memory client tests list exactly the registry's tools with their schemas; each tool returns the same content as a direct dispatch over the same session; malformed arguments, unknown tools, oversized results, and the session cap return error results; untrusted text stays marked; no command other than `mcp` imports `mcp`.

Implement and verify substeps 45.1–45.3 in order. Keep the parent planned until every substep's acceptance checks pass.

**Which `mcp` API:** the low-level `mcp.server.Server`, with explicit list and call handlers. `MCPServer` derives each tool's schema from a Python signature, which would let the served schemas drift from the registry's declarations. Tests connect through the package's in-memory transport and client, so they need no subprocess or stdio.

**What a connection holds:** its own fresh copy of the selected session (`ToolSession.fresh()`, so the verified data is shared and the run record is not), its call count, and its next call ID. The server is built from the session and limits alone, and nothing lives in module globals, so two connections never see each other's returned IDs.

**What is reused:** `default_registry()` and `ToolRegistry.dispatch` unchanged; `parse_tool_arguments` for argument bounds; the `ToolErrorCode` messages; `SYSTEM_INSTRUCTIONS` as the source of the server's instructions.

### Step 45.1: Build the server and list the tools
**Status:** Implemented. `assistant/mcp_server.py` adds `build_server(session)`, a low-level `mcp.server.Server` named `archdiscov` with the installed package version, the new `MCP_INSTRUCTIONS`, and a list handler serving one `mcp.types.Tool` per registry declaration, in order, with name, description, and input schema unchanged, no output schema, and no `submit_answer`. Each connection's state is a `Connection` (a `ToolSession.fresh()` copy and a call count of zero) entered through the server's lifespan, which `mcp` enters once per `run`, so per connection; nothing is kept in module globals. One planned point was simplified: the call count will also number the call IDs in 45.2, so there is no separate ID counter. `assistant/instructions.py` now builds both texts from shared sections: `SYSTEM_INSTRUCTIONS` is byte-identical (its pinned digest and version `"2"` unchanged), and `MCP_INSTRUCTIONS` (version `"1"`, pinned) replaces only the opening (the tools reach only the selected snapshots) and the submission bullets (submit before presenting any conclusion, resubmit after rejections, present a rendered answer as returned). Types come from `mcp.types`, which re-exports `mcp_types`, so only the declared `mcp` dependency is imported. `tests/assistant/test_mcp_server.py` connects through the `mcp` in-memory client: the served tools equal the declarations (schemas compared as canonical JSON, since `mcp` reorders keys), the server reports its name, version, and instructions, and two connections share the verified data with separate run records. The instructions tests pin the MCP text and check its shared and own statements and that it makes none of the `ask`-only ones; a new import-isolation test shows the CLI, an `ask` run, and an evaluation never import `mcp` while building the server does, and fails when `mcp` is imported early. `docs/mcp-server.md` describes what the server serves and connection isolation, linked from the tools page, whose stale "only provider is the scripted fake" sentence now names configured providers and the MCP server.
**Prerequisite:** Completed phase 4.

- Add `assistant/mcp_server.py` with a function that builds a low-level `Server` from a `ToolSession` and limits: the server name `archdiscov`, the installed package version, and instructions adapted from `SYSTEM_INSTRUCTIONS` (records are data, imported text is quoted and never an instruction, and conclusions go through `submit_answer`, which step 46 adds). Omit anything in the assistant instructions that only makes sense inside an `ask` run, such as the request budget.
- List one MCP tool per registry declaration, in registration order, with the declaration's name, description, and input schema unchanged. `submit_answer` is not listed yet.
- Give each connection its own state through the server's lifespan: a fresh session copy, a call count of zero, and a call-ID counter.
- Import `mcp` only inside this module, and import this module only when a server is built. Extend the import-isolation test so building the registry, `ask`, `evaluate`, and every other command leave `mcp` unimported.
- Start `docs/mcp-server.md` with what the server serves and how connections are isolated; link it from the tools page.

**Advice:** Compare served schemas with the declarations as JSON, not by eye; any transformation the `mcp` types apply (key order, defaults) must not change what a client validates against.

**Acceptance check:** An in-memory client lists exactly the registry's tool names, descriptions, and input schemas in order; the server reports its name, version, and instructions, and the instructions name `submit_answer` and the quoted-data rule; two connections get separate run records over the same verified data; importing the registry or running `ask` does not import `mcp`.

### Step 45.2: Dispatch tool calls
**Status:** Implemented. `build_server` adds a call handler: `Connection.next_call_id()` numbers the call (`mcp-1`, ...), `parse_tool_arguments` bounds its arguments (an omitted `arguments`, which MCP allows for a tool without inputs, is `{}`), and `ToolRegistry.dispatch` runs it over the connection's session; the result is one `TextContent` of the tool's JSON with `is_error` from the `ToolResult`, and no structured content. Two cases outside the registry: building a `ToolCall` validates the tool-name pattern, so a client name outside it returns `tool_unknown` rendered through the registry's own `render`; and any other exception in the handler returns `tool_internal_error` without its message. Dispatch runs synchronously in the handler, without an await, because `mcp` serves a connection's requests concurrently: a mutation that moved it to a worker thread interleaved concurrent calls and failed the ordering test. `tests/assistant/test_mcp_server.py` now runs every README case's transcript calls (from `prepare_evaluation`'s verified sessions) and every tool once through the in-memory client and compares each result with a direct dispatch over a fresh copy of the same session (ignoring an unsaved analysis's `created_at`, which differs per run), and the connection's run record with the direct one; shows an unknown and an impossible name, oversized and invalid arguments, unknown fields, and oversized outputs as error results with their codes, and an argument-less call as a call with none; keeps imported text's untrusted marking (the adversarial injected snapshot); records eight concurrent, slowed calls start-to-end in order; and returns `tool_internal_error` without the exception text. Mutating the omitted-arguments rule, the name handling, or the synchronous dispatch fails a test. `docs/mcp-server.md` gains "Calls" with the error-code table, and a drift test pins the table to `ToolErrorCode` and the 16 KiB bound.
**Prerequisite:** Step 45.1.

- Handle a call by bounding its arguments with `parse_tool_arguments` (the same object-only and size rules as `ask`), building a `ToolCall` with the connection's next call ID (`mcp-1`, `mcp-2`, ...; MCP gives no call ID), and dispatching it through `ToolRegistry.dispatch` over the connection's session.
- Return the `ToolResult` content as one text content item, with `isError` set from `is_error`. A tool error stays a result with its `ToolError` code and fixed message, never a protocol error, so a client can recover. Do not add structured content or output schemas: the text is already the tool's JSON, and a second copy could drift.
- Run one call at a time per connection, so the run record sees calls in order even when a client sends them concurrently. A failure outside a tool (in argument handling or rendering) becomes `tool_internal_error`, and no exception text or traceback reaches the client.

**Advice:** The registry already turns unknown tools, malformed or invalid arguments, oversized outputs, and tool exceptions into `ToolError`s; the server only adds the transport. If a case seems to need new handling here, it belongs in the registry.

**Acceptance check:** Each tool, called through the in-memory client with the arguments the README examples use, returns exactly the content of a direct dispatch over a fresh copy of the same session, and its returned IDs reach the connection's run record; an unknown tool, malformed and oversized arguments, invalid arguments, and an oversized output return error results with their codes; quoted imported text keeps its untrusted marking; concurrent calls on one connection are recorded in order; an unexpected exception returns `tool_internal_error` without its message.

### Step 45.3: Bound a server session
**Status:** Implemented. `ServerLimits` in `assistant/mcp_server.py` holds `max_tool_calls` (strict, default 200, 1–2,000); `build_server(session, limits=None)` gives each `Connection` the limits, and a call past the cap returns the new `tool_session_limit_reached` (`ToolErrorCode.SESSION_LIMIT_REACHED`, with a fixed message, rendered through the registry's `render`) before its arguments are parsed, so nothing is dispatched or recorded; every call counts, errors included, and the count keeps numbering call IDs. The default was settled against the README examples: answering all nine in one connection takes 33 calls, submissions included, and a test requires the default to allow at least four times that. Per-call `ToolLimits` come from the session, as in `ask`. Tests reach the cap exactly (three dispatches, then two refused calls that dispatch and record nothing), give a second connection its full count, refuse out-of-range and non-integer limits, and truncate at the session's result limit exactly as a direct dispatch; mutating the cap comparison fails a test. The new code is documented on the tools page (MCP-only, since `ask` bounds calls with run limits), and `docs/mcp-server.md` gains "Limits", the served tool names, and the code; drift tests pin the served tool names to the registry, the per-call defaults to `ToolLimits`, and the cap's default and bounds to `ServerLimits`.
**Prerequisite:** Step 45.2.

- Add a session-wide cap on tool calls per connection, counting every call (errors included, and `submit_answer` once step 46 adds it). Propose a default of 200 calls, between 1 and 2,000, and settle it in this substep against the README examples' needs. Past the cap, every call returns an error result with a new `tool_session_limit_reached` code and fixed message, and dispatches nothing.
- Keep the existing `ToolLimits` (result limit, depth, and output size) per call, configured as `ask` configures them.
- Document the limits, the cap, the error codes, and connection isolation in `docs/mcp-server.md`, and add a drift test that pins its tool list, limits, and codes to the code.

**Advice:** Choose the cap so a client answering several questions in one session does not hit it, while a runaway client loop does; it is a backstop, not a budget.

**Acceptance check:** The cap is reached exactly after its count, then every call returns `tool_session_limit_reached` without dispatching or recording; another connection still has its full count; per-call limits truncate as in `ask`; the documented tool list, limits, and codes match the code.

## Step 46: Validate and render answers submitted over MCP
**Status:** Implemented. Substeps 46.1–46.4 pass their acceptance checks: proposal reading and the rejection listing are shared with `ask`, the answer renderings live in `assistant/answer_output.py`, the server serves `submit_answer` and validates and renders submissions as `ask` does, and parity tests and documentation cover the result.
**Prerequisite:** Step 45.

- Offer the `submit_answer` tool with the same proposal schema as `ask`. Validate each submission with `validate_proposal` against the server session's run record (the IDs this session's tools returned). A rejected proposal returns its rejection codes, locations, and fixed messages, so the client can repair it; an accepted one returns the rendered answer, readable and as answer JSON, with the instruction to present it as the answer.
- Each submission is validated independently: there is no single-repair budget, since the client, not the server, drives the conversation, but submissions count toward the session cap.
- Keep the run record session-wide (every ID the session returned may be cited), and say so in the docs.

**Advice:** Do not reimplement validation or rendering for MCP; the same proposal over the same session must give the same rejections or the same rendering as `ask`.

**Acceptance check:** A fabricated, unreturned, or cross-snapshot citation, a status upgrade, and narrative that repeats imported text are rejected with their codes; a valid proposal renders identically to `answer_question` over the same session and proposal; imported text appears only as quoted data; the README example proposals from `tests/support/ask_examples.py` validate after their tool calls.

Implement and verify substeps 46.1–46.4 in order. Keep the parent planned until every substep's acceptance checks pass.

**Why two refactors come first:** the pieces `submit_answer` needs over MCP exist, but not where the server can use them as they are.
- Reading a proposal is private to the orchestrator (`_proposal_outcome`), and the rejection listing is built in `repair.py` together with `ask`-only wording ("It is the only tool offered now, and there is no further repair").
- The readable and JSON answer renderings (`answer_text`, `answer_data`, `display_text`, `redacted_data`) live in `cli_answer_output.py`; the server is under `assistant/`, and importing a CLI module there would invert the layering.

**What stays the same for `ask`:** both refactors move code without changing behaviour. Every `ask` transcript, repair prompt, rendering, and JSON envelope stays byte-identical, and the existing answer, repair, and CLI tests pass unchanged.

**What an MCP submission is:** one `submit_answer` call, validated on its own against the connection's session-wide run record. There is no repair budget and no `answered_after_repair`: an accepted proposal is `answered`, however many rejected ones came before it. Validating and rendering never change the run record.

### Step 46.1: Share proposal reading and the rejection listing
**Status:** Implemented. One planned point changed: `read_proposal(arguments)` lives in `assistant/runs.py`, not `answers.py`, because `runs.py` already imports `answers.py` and owns `MalformedOutputReason`; it returns the existing `Proposed` or `MalformedOutput` outcome (`proposal_malformed` for unreadable arguments, `proposal_invalid` for a proposal that does not validate), and the orchestrator's `_proposal_outcome` now returns it. `repair.py` replaces its private `_submission` with `submission_text(outcome, rejections, follow_up)`: the unreadable reason and its fixed message, or `answer_rejected` with up to `MAX_LISTED_REJECTIONS` codes, locations, and messages and the unlisted count, then `next` set to the follow-up; `repair_prompt` passes `RESUBMIT`, and `PROPOSAL_MIXED` stays a reason only `ask` produces. Neither function imports the orchestrator. A comparison against the previous `repair.py` showed all 18 repair prompts byte-identical (every rejection code, a listing over the bound, the three unreadable reasons, and a plain-text answer), and the orchestrator, answer-service, and acceptance tests pass unchanged. New tests: `tests/assistant/test_runs.py` reads a valid, an unreadable, and an invalid proposal; `tests/assistant/test_repair.py` lists rejections up to the bound with another follow-up, names each unreadable reason with it, and shows the repair prompt is `submission_text` with `RESUBMIT`.
**Prerequisite:** Step 45.

- Extract the orchestrator's proposal reading into a public function in `assistant/answers.py`: from `ToolArguments` to an `AnswerProposal`, or the `MalformedOutputReason` (`proposal_malformed` or `proposal_invalid`) a repair reports. The orchestrator calls it unchanged.
- Split the submission result in `repair.py` into a body (the unreadable reason and its fixed message, or `answer_rejected` with up to `MAX_LISTED_REJECTIONS` codes, locations, and fixed messages and the unlisted count) and the follow-up line. `ask` keeps `RESUBMIT` as its follow-up; the MCP server will pass its own (step 46.3). `PROPOSAL_MIXED` stays `ask`-only, since an MCP call is always one call.

**Advice:** Keep the extracted functions free of transport and conversation types beyond what they read, so the MCP server needs no orchestrator import.

**Acceptance check:** The orchestrator and repair tests pass unchanged, and the repair prompts for every rejection code and unreadable reason are byte-identical to before; new unit tests cover the shared reader (a valid, a malformed, and an invalid proposal) and the body with another follow-up line, including the listing bound.

### Step 46.2: Move the answer renderings below the CLI
**Status:** Implemented. `assistant/answer_output.py` now holds `answer_text`, `answer_data`, `display_text`, `redacted_data`, `ANSWER_JSON_VERSION`, and their private helpers, moved unchanged: every moved definition's syntax tree equals the original's in `cli_answer_output.py` at the previous commit. `cli_answer_output.py` keeps only `print_answer`, and `cli_ask_output.py` and `cli_evaluation_output.py` import the rest from the new module. One planned point changed: the tests that imported these names from `cli_answer_output` (the answer-output, ask-CLI, answer-service-suite, and continuation tests, the import-isolation script, and the drift test pinning the answer JSON version) now import them from where they live, with every assertion unchanged, rather than through re-exports. A new import-isolation test shows `assistant/answer_output.py` loads neither Typer nor any CLI module, and fails when Typer is imported there. `docs/assistant-answers.md` names the new module; `docs/project-layout.md` does not name either module. The offline and acceptance suites pass, so every `ask` and `evaluate` rendering is unchanged.
**Prerequisite:** Step 46.1.

- Move `answer_text`, `answer_data`, `display_text`, `redacted_data`, and `ANSWER_JSON_VERSION`, which do not depend on Typer, from `cli_answer_output.py` into a new `assistant/answer_output.py`. `cli_answer_output.py` keeps `print_answer` and imports the rest, and `cli_ask_output.py` and `cli_evaluation_output.py` import from the new module.
- Update the docstrings and `docs/project-layout.md` if it names the module.

**Advice:** This is a move, not a rewrite: diff the moved functions against the originals, and keep the documentation-drift tests that pin the answer JSON contract pointing at the moved code.

**Acceptance check:** Every `ask` and `evaluate` rendering, readable and JSON, is byte-identical to before (the existing CLI and acceptance tests pass unchanged); `assistant/answer_output.py` imports nothing from the CLI or Typer, checked by a test.

### Step 46.3: Serve `submit_answer`
**Status:** Implemented. `answers.py` now builds both descriptions from shared citation rules, side by side: `ASK_SUBMIT_DESCRIPTION` (byte-identical to the previous text, checked against the last commit) and `MCP_SUBMIT_DESCRIPTION` (submit before presenting any conclusion, resubmit after a rejection, present an accepted answer as returned); `submit_answer_declaration(description=ASK_SUBMIT_DESCRIPTION)` keeps one schema for both. The server lists `submit_answer` last with that schema and the MCP description, and handles it in `_dispatch` after the cap check and argument bounds and before a `ToolCall` is built: `read_proposal` and `validate_proposal` over the connection's session, an unreadable or rejected proposal returning `submission_text(..., SUBMIT_FOLLOW_UP)` as an error result, and an accepted one rendered with `render_answer` and `answered` and returned as three text items (`PRESENT_ANSWER`, `answer_text`, and `answer_data` as indented JSON). Calls now return a small `_Reply` of text items and an error flag, since a submission returns three items and a tool call one. `MCP_INSTRUCTIONS` is unchanged: the served description repeats, rather than replaces, its submission rules. `tests/assistant/test_mcp_server.py` lists `submit_answer` last with `ask`'s schema and without "exactly once"; accepts the ownership example's proposal after its tool calls (the present line, the readable heading, and an `answered` outcome) and rejects it before them (`answer_unknown_id`); returns `proposal_malformed` and `proposal_invalid` with the follow-up; answers a submission accepted after a rejected one as `answered`; counts submissions toward the cap; and leaves the run record as the tool calls left it. Moving the submission ahead of the cap check fails a test. `docs/mcp-server.md` lists the served `submit_answer` and gains "Answers"; the drift test that pins the served tool names follows the new text.
**Prerequisite:** Step 46.2.

- List `submit_answer` after the registry's tools, with the proposal's input schema from `submit_answer_declaration()` unchanged and an MCP description (submit before presenting any conclusion; correct and resubmit after rejections; present an accepted answer as returned) instead of the `ask` one, which says to submit exactly once. Keep both descriptions in `answers.py`, side by side.
- Handle a `submit_answer` call in the server, not the registry, since it is never dispatched as a tool:
  - count it toward the connection's call cap first, like every call;
  - bound its arguments as any call's and read them with the shared reader; an unreadable proposal is an error result with the shared body and an MCP follow-up line ("Correct the answer and submit it again.");
  - validate it with `validate_proposal` over the connection's session; rejections are an error result with the shared body and the same follow-up;
  - render an accepted proposal with `render_answer` and the outcome `answered`, and return a non-error result of two text items: the readable answer (`answer_text`) and the answer JSON (`answer_data`), preceded by one fixed line telling the client to present the answer as returned.
- Update the server instructions only if the served description makes a statement in them redundant; any change to `MCP_INSTRUCTIONS` bumps `MCP_INSTRUCTIONS_VERSION`.

**Advice:** The readable answer is `ask`'s, byte for byte, including its evidence-inspection hint; do not tailor it for MCP, so the two cannot drift.

**Acceptance check:** In-memory client tests list `submit_answer` last with the proposal schema; a README example's proposal, submitted after its tool calls, returns the readable and JSON answer, and the same proposal submitted before them is rejected for unreturned IDs; malformed and invalid proposals return their reasons; a rejected and then an accepted submission give `answered`; submissions count toward the cap; the run record is unchanged by submitting.

### Step 46.4: Prove parity with `ask` and document answers
**Status:** Implemented. `tests/assistant/test_mcp_answers.py` goes beyond the planned five rejections: every rejection scenario of the answer suite (one per `RejectionCode`, so including the cross-snapshot citation over its comparison session) and the adversarial suite's fabricated finding, unreturned evidence, status upgrade, and repeated-imported-text proposals are submitted through the server after their tool calls, and the served rejection codes and locations equal `validate_proposal`'s after the same calls over a fresh session copy, each with its expected code. Every README example and the injected-text case, from `prepare_evaluation`'s verified sessions and the checked-in transcripts, are accepted with the readable answer equal to `answer_question`'s `answer_text` and the JSON equal to its `answer_data`, with the fake provider replaying the same transcript; the injected text appears only as quoted data and on `imported:` lines. Rendering with `answered_after_repair` instead of `answered` fails the parity test. `docs/mcp-server.md` "Answers" now states the proposal contract, the two MCP sentences that replace `ask`'s "exactly once", a result-shape table (unreadable, rejected, and accepted), the follow-up and present lines, the session-wide run record, no repair budget and the `answered` outcome, the parity tests, and what the server cannot enforce (a client's own text, or changes it makes to an accepted answer). A new drift test pins the quoted sentences to `MCP_SUBMIT_DESCRIPTION`, the two lines to `PRESENT_ANSWER` and `SUBMIT_FOLLOW_UP`, and each documented result shape to the keys `submission_text` produces and the listing bound; changing the follow-up line fails it.
**Prerequisite:** Step 46.3.

- Run the adversarial proposals through the server after their tool calls, from the checked-in adversarial suite where it has them: a fabricated finding ID (`answer_unknown_id`), unreturned evidence (`answer_id_not_returned`), a status upgrade (`answer_status_upgrade`), and narrative that repeats imported text (`answer_narrative_imported_text`); add a cross-snapshot citation (`answer_other_snapshot_id`) over a comparison session, reusing the answer suite's scenario.
- Compare with `ask`: for every README example and the injected-text case, the accepted rendering equals `answer_question`'s over the same session and proposal with the fake provider, readable and JSON; for every rejected proposal, the rejection codes and locations equal `validate_proposal`'s.
- Document answers in `docs/mcp-server.md`: the `submit_answer` contract, the result shapes, the session-wide run record, no repair budget and the `answered` outcome, and what the server cannot enforce (a client's own text outside a submitted answer). Extend the drift tests to the served description, the follow-up line, and the result shapes.

**Advice:** Reuse the evaluation fixtures and the answer suites' scenarios rather than writing new proposals; a parity test is only as good as the variety of proposals it compares.

**Acceptance check:** Every adversarial proposal is rejected with its code over MCP exactly as `validate_proposal` rejects it; every accepted rendering equals `ask`'s; imported text appears only as quoted data in an accepted answer; the documentation states the cannot-enforce limit, and its contracts match the code.

## Step 47: Add the `archdiscov mcp` command
**Status:** Implemented. Substeps 47.1–47.4 pass their acceptance checks: the README questions are a production list offered as prompts, `services/mcp.py` verifies a selection and serves it over stdio (with its own standard-input reader so an interrupt stops a waiting server), `archdiscov mcp` routes to it with the `ask` diagnostics and exit codes 0, 1, 2, and 130, and the page documents running the server and setting up Claude Code and Claude Desktop.
**Prerequisite:** Step 46.

- Add `archdiscov mcp --snapshot PATH [--compare PATH] [--comparison-result PATH ...] [--max-tool-calls N]`, which loads and verifies the selected files with the `ask` diagnostics and exit codes (1, 2) before serving, then serves over stdio until the client disconnects (0) or it is interrupted (130).
- Reserve standard output for the protocol: diagnostics before serving and any logging go to standard error, and nothing in a diagnostic or log holds snapshot content, a question, or a proposal.
- Offer the five README example questions as MCP prompts, one per question, named after the analyzer each one leads to and taking no arguments: each prompt's text is the question exactly as the README states it, so a client can start a supported question in one step. The prompts add no instructions beyond the server's own.
- Document client setup for Claude Code (`claude mcp add`) and Claude Desktop, with paths chosen at configuration time.

**Advice:** Keep snapshot selection fixed at startup: a tool that opens arbitrary paths would turn a read-only analysis server into a filesystem reader.

**Acceptance check:** CLI tests cover each pre-serve diagnostic and exit code, a clean disconnect, and an interrupt; a stdio client session through the command lists the tools and the five prompts, gets each prompt's question exactly as the README states it, and answers a README example; standard output carries only protocol messages.

Implement and verify substeps 47.1–47.4 in order. Keep the parent planned until every substep's acceptance checks pass.

**Where things go:**
- The README questions become a production constant in a new `assistant/questions.py`, which imports no `mcp`, so the prompts, the tests, and the README check share one list.
- Serving over stdio lives in `services/mcp.py`, which loads the selection and imports `assistant/mcp_server.py` only once the files are verified.
- The command lives in `cli_mcp.py`, which stays routing and presentation, and imports the service lazily so no other command loads `mcp`.

**What is reused:**
- `ToolSession.from_paths` for the selection, as `ask_question_async` calls it, and `safe_assistant_failures` for the pre-serve diagnostics and exit codes.
- `mcp.server.stdio.stdio_server`, which already points file descriptor 1 at standard error while serving, so stray output from a handler or library cannot reach the protocol stream.
- `ServerLimits` for `--max-tool-calls`, its bounds becoming the option's bounds.
- The `mcp` package's stdio client (`StdioServerParameters`, `stdio_client`) as the test client for the real command.

**What the tests cannot cover in process:** a stdio session needs the command's own standard input and output, so protocol tests run the command in a subprocess; diagnostics and exit codes before serving stay in-process tests with Typer's runner.

### Step 47.1: Offer the README questions as prompts
**Status:** Implemented. `assistant/questions.py` holds `README_QUESTIONS`, a read-only mapping from analyzer kind to its README question in README order, importing only the models; `tests/support/ask_examples.py` derives its question constants and tuple from it, and `test_the_examples_cover_every_readme_question_and_status` compares the README's list with the production mapping. The server lists one prompt per question, named by the analyzer kind, with a fixed description and no arguments; getting one returns a single user message with the question and no description. An unknown name, or any arguments, raises `MCPError(INVALID_PARAMS, UNKNOWN_PROMPT)` with a fixed message that never echoes the request; since analyzer kinds are string enums, the lookup is a plain `get` by name. In-memory client tests check the listing order, names, and absent arguments, each prompt's single message, and the fixed error for an unknown name and for arguments; the import-isolation test now loads `assistant/questions.py` before asserting `mcp` stays unimported. `docs/mcp-server.md` gains "Prompts", and a drift test pins the documented names to `README_QUESTIONS` and the error message to `UNKNOWN_PROMPT`.
**Prerequisite:** Step 46.

- Add `assistant/questions.py` with the five README example questions, each keyed by the analyzer kind it leads to (`internet_exposure`, `database_access`, `security_group_impact`, `cross_account_trust`, `ownership`), in the README's order. `tests/support/ask_examples.py` takes its question constants from it, and the existing check that reads the README's question list now compares it with the new constant.
- List one MCP prompt per question, named by the analyzer kind, with a short fixed description and no arguments. Getting a prompt returns one user message whose text is the question exactly; the prompt adds no instructions of its own.
- An unknown prompt name, or arguments for a prompt that takes none, is an MCP invalid-params error with a fixed message that does not echo the input.

**Advice:** Keep the questions in one place: if the README's wording changes, the README check fails until the constant follows, and the prompts follow the constant.

**Acceptance check:** In-memory client tests list exactly five prompts, in README order and named by analyzer kind, with no arguments; each prompt returns its README question as one user message and nothing else; an unknown name and unexpected arguments return the fixed error without the input; the README check now uses the production constant, and `assistant/questions.py` imports no `mcp`.

### Step 47.2: Serve the selection over stdio
**Status:** Implemented. `services/mcp.py` adds `serve_selection(snapshot_path, comparison_path=None, comparison_result_paths=(), limits=None)`, which verifies the selection with `ToolSession.from_paths` (the `ask` errors: `SnapshotIOError`, `SnapshotValidationError`, `ToolSessionError`) and then runs `serve_session` with `asyncio.run`; `serve_session(session, limits, transport=None)` imports `mcp`'s `stdio_server` and the server module only then, builds the server, holds the `mcp` loggers at `WARNING` with the adapters' `quiet_loggers`, and serves over the transport (stdio by default) until the client closes it. An interrupt propagates as `KeyboardInterrupt` after `stdio_server` restores the descriptors. One unplanned move: `ServerLimits` now lives in a new `assistant/mcp_limits.py`, which does not import `mcp`, because the service and the 47.3 command need it before any server is built and `cli.py` imports every command module at startup; `mcp_server.py`, its tests, and the drift test import it from there. `tests/services/test_mcp_service.py` shows, in a subprocess, a missing snapshot, an invalid snapshot, and a saved comparison of unselected snapshots failing with their errors while `mcp` stays unimported; serves the ownership example over in-memory streams through its tool calls and an accepted `submit_answer`, then returns once the client closes, with the `mcp` logger raised from DEBUG to WARNING while serving (removing the quieting fails it); passes the verified selection and limits to `serve_session`; and lets an interrupt reach the caller. `docs/mcp-server.md` gains "Serving" and names the limits module.
**Prerequisite:** Step 47.1.

- Add `services/mcp.py` with a function that takes the selected paths and `ServerLimits`, loads and verifies the selection with `ToolSession.from_paths` (raising the same errors as `ask` before anything is served), then builds the server and serves it with `stdio_server` until the client closes the connection.
- Hold the `mcp` package's loggers at `WARNING`, as the Bedrock adapter holds `boto3`'s, so no log line carries a request, a tool result, or a proposal; any log goes to standard error.
- Let an interrupt end serving: the function returns normally when the client disconnects, and lets `KeyboardInterrupt` reach the caller after `stdio_server` has restored the standard descriptors.

**Advice:** Do not catch broad exceptions around serving: a failure inside a call is already a tool error (step 45.2), so anything that escapes is a bug and should stop the process with a diagnostic, not hang it.

**Acceptance check:** Service tests show a missing snapshot, an invalid snapshot, and a saved comparison of unselected snapshots fail before any server is built (with `mcp` still unimported), and that serving over explicit in-memory streams answers a README example and returns when the client closes; the logger levels are set while serving.

### Step 47.3: Add the `archdiscov mcp` command
**Status:** Implemented. `cli_mcp.py` adds `archdiscov mcp --snapshot PATH [--compare PATH] [--comparison-result PATH ...] [--max-tool-calls N]`, registered in `cli.py`: `--max-tool-calls` takes the new `DEFAULT_MAX_TOOL_CALLS`, `MIN_MAX_TOOL_CALLS`, and `MAX_MAX_TOOL_CALLS` from `assistant/mcp_limits.py`, which `ServerLimits` now uses too; the service is imported inside the command; pre-serve errors go through `safe_assistant_failures` as readable diagnostics on standard error (exit 1 or 2), there is no `--json`, a disconnect exits 0, and a `KeyboardInterrupt` exits 130. One problem found and fixed in `services/mcp.py`: with `stdio_server`'s own stdin reader, SIGINT hung the process, because `asyncio.run` cancels the serving task but anyio waits for its worker thread, blocked in `readline()`, until the client's next line. The service now passes its own `_StdinLines` as `stdin`: a daemon thread reads a duplicate of descriptor 0 with `os.read`, splits and decodes lines as `stdio_server` does, and queues them, so a cancelled read stops at once; reading without a Python file object avoids a fatal error at shutdown from a blocked thread holding `sys.stdin`'s buffer lock. Standard output is still claimed and diverted by `stdio_server`. `tests/cli/test_mcp_cli.py` covers in process a missing, an invalid, and an unselected-comparison selection (exit 1, 2, 2, nothing on standard output), out-of-range and non-integer caps (usage, 2), the selection and cap reaching the service, and an interrupt (130); in a subprocess, the `mcp` stdio client lists the tools and the five prompts, gets each README question, and answers the ownership example; a raw session, answering each request before the next, shows every standard-output line is a JSON-RPC result and exits 0 on disconnect; and SIGINT to a server waiting for input, with standard input still open, exits 130 (with `stdio_server`'s reader it times out). `tests/services/test_mcp_service.py` tests the line splitting (across reads, without a final newline, invalid UTF-8, and empty input), and the import-isolation script now runs `archdiscov mcp --help` and imports the service module with `mcp` still unloaded. `docs/mcp-server.md` "Serving" describes the stdin reader and that in-flight requests are cancelled on disconnect.
**Prerequisite:** Step 47.2.

- Add `archdiscov mcp --snapshot PATH [--compare PATH] [--comparison-result PATH ...] [--max-tool-calls N]` in `cli_mcp.py`: the options mirror `ask`'s selection, and `--max-tool-calls` takes `ServerLimits`' default and bounds. There is no `--json`: standard output belongs to the protocol, so pre-serve diagnostics are always the readable ones, on standard error.
- Exit codes: 1 and 2 for file and input errors before serving, as in `ask`; 0 when the client disconnects; 130 when interrupted. A usage error is Typer's 2.
- Import the service only inside the command, and extend the import-isolation test so `archdiscov --help` and every other command still leave `mcp` unimported.

**Advice:** Keep the command's help text about what the server reads and that it never writes or reaches a network, so a user configuring a client sees the boundary where they look first.

**Acceptance check:** In-process CLI tests cover each pre-serve diagnostic and its exit code (a missing snapshot, an invalid snapshot, a saved comparison of unselected snapshots, an out-of-range `--max-tool-calls`), with nothing on standard output. Subprocess tests drive the real command with the `mcp` stdio client: it lists the tools and the five prompts, returns each prompt's question, answers a README example through its tool calls and `submit_answer`, and exits 0 when the client disconnects; an interrupt exits 130; a raw stdio session shows every standard-output line is a JSON-RPC message.

### Step 47.4: Document running the server and client setup
**Status:** Implemented. `docs/mcp-server.md` gains "Running the server" (the usage line, an option table, that the selection is fixed at startup and no tool opens another path, writes, or reaches a network, that standard output carries only protocol messages so there is no `--json`, and an exit-code table: 0 disconnect, 1 `snapshot_io_error`, 2 `invalid_snapshot`, `invalid_comparison_result`, `result_snapshot_unselected`, or a usage error, 130 interrupt) and "Client setup". Both setups were checked against the clients' current documentation: Claude Code's `claude mcp add --transport stdio archdiscov -- archdiscov mcp --snapshot /absolute/path/snapshot.json` (everything after `--` is passed to the server; local scope by default, `--scope user` for every project; `claude mcp list` and `/mcp`; prompts appear as `/archdiscov:ownership`), and Claude Desktop's `mcpServers` entry in `claude_desktop_config.json` with the executable's absolute path as `command` (the file's macOS and Windows locations, a full restart, and the server's log file); the page names both sources and that neither can be exercised offline. The ask page links the server. New drift tests pin the option table to the command's options (with `--snapshot` the only required one and no `--json`), the exit codes to 0, 1, 2, and the cancellation code with their error codes, and the client examples to absolute paths, no environment or credentials, and only real `mcp` options; `mcp` joins the commands whose every option must appear in a documented invocation. The README tables follow in step 48.
**Prerequisite:** Step 47.3.

- Add a "Running the server" section to `docs/mcp-server.md`: the command and options, the exit codes, that snapshots are chosen when the server starts and cannot change during a session, and the prompts.
- Document client setup with absolute paths chosen at configuration time: Claude Code (`claude mcp add archdiscov -- archdiscov mcp --snapshot PATH`) and Claude Desktop (an `mcpServers` entry with the command and its arguments). Check both against the clients' current documentation when writing them, since neither can be exercised offline.
- Link the page from the ask page, and extend the drift tests: the documented options and exit codes match the command, the prompt list matches `assistant/questions.py`, and `mcp` joins the commands whose every option must be documented. The README tables follow in step 48.

**Advice:** Show one complete working configuration per client rather than every option; link the full option list instead of repeating it.

**Acceptance check:** The documented command, options, exit codes, and prompts match the code, checked by drift tests; every `archdiscov mcp` invocation in the docs uses only real options; the client examples name absolute snapshot paths and no credentials.

## Step 48: Close phase 5 with acceptance and documentation
**Status:** Implemented. Substeps 48.1–48.4 pass their acceptance checks: an MCP client answers every README example question offline through the installed server with answers equal to `ask`'s, unsupported claims are rejected and corrected, the server opens only the files it was given and makes no network connection, the README and docs describe the server, and the whole suite passes on the host and in Docker without a network.
**Prerequisite:** Step 47.

- Add an installed acceptance test, outside the checkout with the offline guard allowing no connection at all, through both entry points: an MCP stdio client starts `archdiscov mcp` over the checked-in README fixtures, lists the tools and prompts, answers each README example question, started from its prompt, through its tool calls and `submit_answer`, inspects cited evidence through a tool, and is refused for a fabricated citation; a missing snapshot fails before serving.
- Show that the server reads no file beyond those selected at startup, as well as making no network connection.
- `docs/mcp-server.md` already documents the server (steps 45–47). Link it from the README (capability table, command table, delivery table with a phase 5 row, and a phase 5 acceptance section), and correct any page that still calls the assistant or the server later work.
- Run focused and full offline tests, the acceptance suite, Ruff, whitespace checks, and the suite in Docker with networking disabled.

**Advice:** Keep real MCP clients out of CI; the stdio client from the `mcp` package is the test client.

**Completion criterion:** An MCP client can answer every README example question offline through the installed server; every validated answer cites tool-returned findings and evidence and states coverage limitations; fabricated or unreturned citations are always rejected; the server makes no network connection and reads no file beyond those selected at startup.

Implement and verify substeps 48.1–48.4 in order. Keep the parent planned until every substep's acceptance checks pass.

**Where things go:**
- The journeys live in a new `tests/acceptance/test_phase_five_acceptance.py`, one `xdist_group` per entry point, as in phase 4.
- Watching file opens is an opt-in addition to `tests/support/offline_guard.py`, so every installed-CLI test keeps its current environment.

**What is reused:**
- The README and adversarial suites under `tests/fixtures/evaluation/` as the fixtures: each case's snapshot, its transcript's tool calls, and its proposal, read with `tests/support/mcp_clients.py`.
- `Workspace` and the installed-CLI harness in `tests/support/installed_cli.py`, and the `mcp` package's stdio client, as `tests/cli/test_mcp_cli.py` already drives it in a subprocess.
- `README_QUESTIONS` from `assistant/questions.py`, and the `ask --json` answer for the same files and transcript as the parity reference.

### Step 48.1: Answer the README questions through the installed server
**Status:** Implemented. `tests/acceptance/test_phase_five_acceptance.py` runs one journey per entry point outside the checkout, each on its own xdist worker, with the offline guard as the whole `PYTHONPATH`: every case of the README suite is ingested byte for byte, then `archdiscov mcp` serves the ingested files over stdio while a real-process client lists the tools (the registry's, then `submit_answer`) and the five prompts in README order, gets the case's question from its prompt, makes the transcript's tool calls, submits its proposal, and retrieves the first cited evidence ID with `get_evidence`. Every submission is accepted; the readable answer equals what `ask` prints after its run summary and a blank line, and the answer JSON equals `ask --json`'s `result`, for the same files and transcript, so no golden file exists. Cited findings are exactly the suite's expected ones, findings short of `insufficient_evidence` cite evidence, answers short of `supported` state qualifications, the server exits 0 with empty standard error and no standard output beyond its replies, no fixture changes, and the network log stays empty. `ownership_compared` (two snapshots and a saved comparison) has no prompt, since its question is not a README question, and is asked by its text. A new `StdioSession` in `tests/support/mcp_clients.py` drives the process with raw JSON-RPC lines, one reply per request (the `mcp` client cannot show an exit code or standard error), with a watchdog that kills a server that stops answering; `ingest_selection` and `suite_cases` in `tests/support/installed_cli.py` are shared with the phase 4 journey, which now uses them. Breaking the served answer data and changing a prompt's text each fail the journey.
**Prerequisite:** Step 47.

- Add `tests/acceptance/test_phase_five_acceptance.py`. For each entry point, in a workspace outside the checkout with the offline guard allowing no port: ingest each README fixture snapshot with the installed `ingest` command, then start `archdiscov mcp` on it through the `mcp` stdio client.
- The client lists exactly the registry's tools plus `submit_answer`, and the five prompts in README order. For each README example, it gets the question from its prompt, makes the transcript's tool calls, calls `get_evidence` for the first evidence ID the accepted answer cites, and submits the transcript's proposal.
- Each submission is accepted. Its answer JSON equals the installed `ask --json` answer for the same files and transcript, and it cites only findings and evidence the session's tools returned. Answers short of `supported` state their limitations (limited coverage, insufficient evidence, the Terraform plan).
- The server exits 0 when the client disconnects, and the network log stays empty.

**Advice:** Compare with `ask` rather than with a stored golden answer: parity with `ask` is the property this phase promises, and a golden file would need updating whenever a rendering changes.

**Acceptance check:** Through both entry points, every README example question is answered from its prompt with an accepted answer equal to `ask`'s, cited evidence is retrieved through `get_evidence`, limitations are stated, and no connection is attempted.

### Step 48.2: Prove the refusals and the file boundary
**Status:** Implemented. A second journey in `tests/acceptance/test_phase_five_acceptance.py`, one per entry point on the same worker as 48.1's, submits the adversarial suite's four unsupported claims over the installed server: each is rejected as `answer_rejected` with its code (`answer_unknown_id`, `answer_id_not_returned`, `answer_status_upgrade`, `answer_narrative_imported_text`) and a fixed message that repeats neither the fabricated ID nor the injected text, then a corrected proposal is accepted in the same session: the real finding the session's `run_analyzer` returned, the cited evidence and resource retrieved through `get_evidence` and `inspect_resource`, a weaker status, and a plain summary. Each submission stands alone, so the original proposal is still rejected after its correction, except the one whose correction was to retrieve what it cites. The injected-text case shows imported instructions only as quoted data: every occurrence in a tool reply or the accepted answer is the value of an `untrusted_text` key, and none is in the narrative. A missing, an invalid, and an unselected snapshot exit 1, 2, and 2 with their diagnostics on standard error and nothing on standard output. The file boundary is the offline guard's new opt-in `watching(*directories)`, which installs an audit hook that logs every file the child opens under those directories, as real paths and excluding the guard's own logs (`opened_files` reads them); without it the child's environment is unchanged, and `test_installed_end_to_end.py` tests that it is opt-in, follows symbolic links, and logs nothing else. A session over a saved comparison calls every tool the registry declares (each name is checked against the registry, so a new tool must be added), then calls tools with the paths of decoys in place of IDs and with arguments no tool declares, each refused, and submits the proposal; with other cases' ingested snapshots and a snapshot, a saved comparison, and a saved analysis copied beside the selection, the files opened are exactly the three selected, and the server exits 0 with empty standard error. A tool patched to open a decoy and a server patched to skip validation each fail the journey.
**Prerequisite:** Step 48.1.

- From the adversarial suite, submit the fabricated finding, the unreturned evidence citation, the status upgrade, and the injected narrative. Each is rejected with its rejection code, and a corrected proposal is then accepted in the same session, since the server has no repair budget. Imported instructions appear in tool results and answers only as quoted data.
- A missing snapshot exits 1 and an invalid one exits 2, each with its diagnostic on standard error, nothing on standard output, and no server started.
- Add an opt-in file watch to the offline guard: an audit hook in its `sitecustomize` logs every file opened under a watched directory. Put an unselected decoy snapshot and a decoy saved comparison next to the selected files, run a full session (every tool, then `submit_answer`), and assert that the only files opened under the workspace are the selected ones.

**Advice:** Watch only the workspace, not the whole filesystem: Python and its libraries open their own files while running, and those are not what the boundary is about.

**Acceptance check:** Each adversarial submission is rejected with its code and a corrected one is accepted; the pre-serve failures exit 1 and 2 with nothing on standard output; the file log lists only the selected files, and a test-only patch that makes a tool open the decoy makes the test fail.

### Step 48.3: Document phase 5 in the README and correct stale pages
**Status:** Implemented. The README gains a capability row and an `mcp` command row linking `docs/mcp-server.md`, a phase 5 row in the delivery table (complete, steps 45–48, linking the new acceptance section), a paragraph in "Assistant behavior" that states the server's limit (it validates what a client submits but cannot stop a client from stating unvalidated claims in its own text), a "Phase 5 offline acceptance" section with the host and Docker commands (the Docker run, the last verified test count, and the closing statement stay for step 48.4), and three acceptance-scenario rows (the README questions answered from their prompts, unsupported claims rejected and corrected, and only the selected files read with no network), with the phase 5 journey's tests added to the coverage-limitations and imported-text rows. Stale text is corrected: `docs/analyzers.md` no longer calls the assistant a later phase, `docs/assistant-answers.md` says the server returns the same forms, `docs/mcp-server.md` no longer says it describes what is implemented so far, and the README names phase 5 beside phases 3 and 4. New drift tests check that the README lists and links the command, names the acceptance test file and section, and has the delivery and capability rows, and that no page calls the assistant, the providers, or the MCP server later work; the scenario-mapping test lists the new rows, whose referenced tests must exist and assert an outcome. Reintroducing a stale phrase and renaming the `mcp` row each fail them.
**Prerequisite:** Step 48.2.

- In the README, add a capability row for the MCP server and an `mcp` row to the command table, both linking `docs/mcp-server.md`. Mark phase 5 complete (steps 45–48) in the delivery table.
- Mention the server in "Assistant behavior", including its honest limit: the server validates what a client submits, but cannot stop a client from stating unvalidated claims in its own text.
- Add a "Phase 5 offline acceptance" section with the host and Docker commands, and add acceptance-scenario rows: README questions answered through an MCP client, fabricated and unreturned citations rejected over MCP, and no network connection and no unselected file read.
- Correct stale text: `docs/analyzers.md` still calls the assistant and providers "a later phase". The plan's opening paragraph still calls phase 5 planned.
- Extend the drift tests: the README links `docs/mcp-server.md` and names `tests/acceptance/test_phase_five_acceptance.py`, each new scenario row names tests that exist, and the stale phrases join `test_implemented_commands_are_not_listed_as_planned`.

**Advice:** Link `docs/mcp-server.md` rather than repeating it in the README. The README states what the server is and where to read more, as it does for `ask`.

**Acceptance check:** The drift and link tests pass with the new rows and sections, each scenario row names existing tests, and no page calls the assistant or the MCP server later work.

### Step 48.4: Verify and close phase 5
**Status:** Implemented. The focused phase 5 and documentation tests, the full offline suite (7,062 tests, 52 skipped), the acceptance suite (491 tests), Ruff (check and format), and the whitespace check pass on the host, and `docker compose build dev` followed by the full suite in `archdiscov-dev` with `--network none` passed all 7,553 tests (52 skipped), the sum of the two host runs; the README's phase 5 section records that count. The image was rebuilt first, since `uv.lock` had gained `pytest-cov` and `pip-audit`. Steps 48 and phase 5 are marked implemented. The phase 4 open questions are closed with what settled them, and the deferred HTTP transport is carried forward as the phase 5 question for a later phase.
**Prerequisite:** Step 48.3.

- Run the focused and full offline tests, the acceptance suite, Ruff, whitespace checks, and the full suite in Docker with `--network none`. Record the test count of the last verified run in the README, as phase 4 does.
- Mark steps 48 and phase 5 implemented, and record what the steps settled: the run record is session-wide (step 46), and the README questions are prompts (step 47). Carry the deferred HTTP transport forward as an open question for a later phase.
- Close the phase 4 open questions that steps 36–37 settled (the default limits and narrative policy) and step 40 settled (the first provider).

**Advice:** Close the phase only on a clean Docker run with networking disabled: the host run cannot show that nothing needed the network.

**Acceptance check:** Every check passes on the host and in Docker without a network; the plan, the README delivery table, and the open questions agree that phase 5 is complete.

## Phase 5 decisions and open questions

**Advice:** The server is a transport for the phase 4 tools, not a second assistant: validation, rendering, and limits stay in one place.

**Open questions (settle before the named step):**
- Settled (step 46): the run record is session-wide, since an MCP client has no question boundary the server can see.
- Settled: the five README questions are offered as MCP prompts (step 47).
- Carried forward to a later phase (phase 6 does not take it up): is a streamable HTTP transport ever wanted? Deferred until a concrete need. Stdio covers local clients (Claude Code, Claude Desktop, IDEs), including a server run in Docker with `docker run -i`. HTTP is only needed for a shared or remote server, and then brings authentication (the MCP authorization flow), TLS, origin checks against DNS rebinding, per-client sessions and limits, and server-side snapshot selection, over data that describes an architecture's attack surface. Step 45 keeps the server transport-independent so the decision stays cheap; settle it in a later phase if a shared server is wanted.
