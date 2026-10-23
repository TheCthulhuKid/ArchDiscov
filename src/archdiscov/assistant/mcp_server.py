"""An MCP server over the assistant tools, for an MCP client to be the model.

The server lists the same tools `ask` offers, from the default registry, with their names,
descriptions, and input schemas unchanged, then `submit_answer`, and gives clients the MCP
instructions. A call is bounded and dispatched as `ask` dispatches one, and its result is
the tool's JSON as text, a tool error included, so a client can recover from it. A
submission is read and validated as `ask` validates a proposal, against the connection's
run record: a rejected one returns `ask`'s rejection listing, and an accepted one returns
`ask`'s readable and JSON answer, `answered` however many rejections came before. The
README's example questions are offered as prompts without arguments, each named by the
analyzer kind that answers it and returning only the question.

Each call is bounded by the session's per-call `ToolLimits`, and each connection by a cap
on its tool calls. Each connection works on its own fresh copy of the selected session:
the verified snapshot data is shared, the run record of returned IDs is not, so one
connection never sees another's returned IDs. The server keeps no state outside a
connection, so it does not depend on the transport that serves it.

This is the only module that imports `mcp`; import it only to build a server.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from .answer_output import answer_data, answer_text
from .answer_rendering import render_answer
from .answer_results import AnswerOutcome, AnswerStatus
from .answer_validation import validate_proposal
from .answers import MCP_SUBMIT_DESCRIPTION, SUBMIT_ANSWER, submit_answer_declaration
from .conversation import ToolArguments, ToolCall, ToolResult, parse_tool_arguments
from .instructions import MCP_INSTRUCTIONS
from .mcp_limits import ServerLimits
from .questions import README_QUESTIONS
from .repair import submission_text
from .runs import MalformedOutput, read_proposal
from .tools.catalog import default_registry
from .tools.contracts import ToolErrorCode, render, tool_error
from .tools.registry import ToolRegistry
from .tools.session import ToolSession

SERVER_NAME = "archdiscov"
# Claude Code moves a tool result over 50,000 characters out of the conversation into a file
# unless the tool declares a larger limit; an accepted answer must reach the model inline,
# so `submit_answer` declares Claude Code's ceiling. Other clients ignore the key.
ANSWER_RESULT_META = {"anthropic/maxResultSizeChars": 500_000}
# What a client is told after a submission is not accepted, and before an accepted answer.
SUBMIT_FOLLOW_UP = "Correct the answer and submit it again."
# Fixed, so an invalid prompt request is never echoed back.
UNKNOWN_PROMPT = "No prompt with that name takes these arguments."
PRESENT_ANSWER = (
    "The answer was accepted. Present it to the user as it is returned below, without "
    "adding conclusions to it."
)


@dataclass
class Connection:
    """What one client connection holds: its session copy, its limits, and how many calls it
    made, which also numbers its call IDs (MCP gives none)."""

    session: ToolSession
    limits: ServerLimits
    calls: int = 0

    def next_call_id(self) -> str:
        self.calls += 1
        return f"mcp-{self.calls}"


@dataclass(frozen=True)
class _Reply:
    """What a call returns: its text items, and whether it failed."""

    texts: tuple[str, ...]
    is_error: bool = False

    @classmethod
    def of(cls, result: ToolResult) -> "_Reply":
        return cls((result.content,), result.is_error)


def _tools(registry: ToolRegistry) -> list[types.Tool]:
    submit = submit_answer_declaration(MCP_SUBMIT_DESCRIPTION)
    tools = [
        types.Tool(
            name=declaration.name,
            description=declaration.description,
            input_schema=dict(declaration.input_schema),
        )
        for declaration in registry.declarations()
    ]
    answer = types.Tool(
        name=submit.name,
        description=submit.description,
        input_schema=dict(submit.input_schema),
        meta=ANSWER_RESULT_META,
    )
    return [*tools, answer]


def _prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name=kind.value,
            description=f"The README example question the {kind.value} analyzer answers.",
        )
        for kind in README_QUESTIONS
    ]


def _prompt(params: types.GetPromptRequestParams) -> types.GetPromptResult:
    """The question a prompt names, as the user's message; a prompt takes no arguments."""
    # Analyzer kinds are string enums, so a prompt's name looks its question up directly.
    question = README_QUESTIONS.get(params.name)
    if question is None or params.arguments:
        raise MCPError(types.INVALID_PARAMS, UNKNOWN_PROMPT)
    content = types.TextContent(type="text", text=question)
    return types.GetPromptResult(messages=[types.PromptMessage(role="user", content=content)])


def _error(call_id: str, code: ToolErrorCode, connection: Connection) -> _Reply:
    result, _ = render(call_id, tool_error(code), connection.session.limits)
    return _Reply.of(result)


def _submit(arguments: ToolArguments, connection: Connection) -> _Reply:
    """Validate a proposal against the connection's run record; render it if accepted."""
    outcome = read_proposal(arguments)
    if isinstance(outcome, MalformedOutput):
        return _Reply((submission_text(outcome, (), SUBMIT_FOLLOW_UP),), is_error=True)
    if rejections := validate_proposal(outcome.proposal, connection.session):
        return _Reply((submission_text(outcome, rejections, SUBMIT_FOLLOW_UP),), is_error=True)
    rendered = render_answer(
        outcome.proposal, connection.session, AnswerOutcome(status=AnswerStatus.ANSWERED)
    )
    # Compact, since the readable answer beside it is the form a person reads.
    data = json.dumps(
        answer_data(rendered), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return _Reply((PRESENT_ANSWER, answer_text(rendered), data))


def _dispatch(
    registry: ToolRegistry, connection: Connection, params: types.CallToolRequestParams
) -> _Reply:
    call_id = connection.next_call_id()
    if connection.calls > connection.limits.max_tool_calls:
        return _error(call_id, ToolErrorCode.SESSION_LIMIT_REACHED, connection)
    # MCP lets a call without inputs omit its arguments.
    arguments = parse_tool_arguments({} if params.arguments is None else params.arguments)
    # `submit_answer` is never dispatched as a tool, here as in `ask`.
    if params.name == SUBMIT_ANSWER:
        return _submit(arguments, connection)
    try:
        call = ToolCall(id=call_id, name=params.name, arguments=arguments)
    except ValidationError:
        # Only the client's name can fail: one outside the tool-name pattern names no tool.
        return _error(call_id, ToolErrorCode.UNKNOWN_TOOL, connection)
    return _Reply.of(registry.dispatch(connection.session, call))


def build_server(session: ToolSession, limits: ServerLimits | None = None) -> Server[Connection]:
    """A server over `session`'s verified data; each connection gets a fresh run record and
    the full `limits`."""
    limits = limits or ServerLimits()
    registry = default_registry()
    tools, prompts = _tools(registry), _prompts()

    @asynccontextmanager
    async def connection(_: Server[Connection]) -> AsyncIterator[Connection]:
        yield Connection(session.fresh(), limits)

    async def list_tools(
        _: ServerRequestContext[Connection], __: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def list_prompts(
        _: ServerRequestContext[Connection], __: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=prompts)

    async def get_prompt(
        _: ServerRequestContext[Connection], params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        return _prompt(params)

    async def call_tool(
        context: ServerRequestContext[Connection], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        state = context.lifespan_context
        # Dispatch runs here, without an await, so calls on a connection never interleave
        # and the run record sees them in the order they arrived.
        try:
            reply = _dispatch(registry, state, params)
        except Exception:
            reply = _error(f"mcp-{state.calls}", ToolErrorCode.INTERNAL_ERROR, state)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text) for text in reply.texts],
            is_error=reply.is_error,
        )

    return Server(
        SERVER_NAME,
        version=version("archdiscov"),
        instructions=MCP_INSTRUCTIONS,
        lifespan=connection,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
    )
