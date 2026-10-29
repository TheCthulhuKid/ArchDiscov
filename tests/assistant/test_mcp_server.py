"""The MCP server over the assistant tools: what it lists, what each connection holds, and
how it dispatches calls.

Clients connect through the `mcp` package's in-memory transport, so no subprocess, stdio,
or network is involved. Every call's result is compared with a direct dispatch through the
registry over a fresh copy of the same verified session.
"""

import asyncio
import json
import time
from importlib.metadata import version
from typing import Any

import pytest
from mcp import Client, types
from mcp.shared.exceptions import MCPError

from archdiscov.assistant import mcp_server
from archdiscov.assistant.answers import MCP_SUBMIT_DESCRIPTION, submit_answer_declaration
from archdiscov.assistant.conversation import MAX_TOOL_ARGUMENT_BYTES, ToolCall
from archdiscov.assistant.conversation import ParsedToolArguments as Parsed
from archdiscov.assistant.instructions import MCP_INSTRUCTIONS
from archdiscov.assistant.mcp_limits import ServerLimits
from archdiscov.assistant.mcp_server import (
    ANSWER_RESULT_META,
    PRESENT_ANSWER,
    SERVER_NAME,
    SUBMIT_FOLLOW_UP,
    UNKNOWN_PROMPT,
    Connection,
    build_server,
)
from archdiscov.assistant.questions import README_QUESTIONS
from archdiscov.assistant.tools.catalog import ALL_TOOLS, default_registry
from archdiscov.assistant.tools.contracts import IdKind, ToolLimits
from archdiscov.assistant.tools.registry import ToolRegistry
from archdiscov.assistant.tools.session import ToolSession
from tests.paths import FIXTURES_ROOT
from tests.support.evaluation_suites import README_SUITE
from tests.support.mcp_clients import Call, mcp_replies, readme_example

SNAPSHOT = FIXTURES_ROOT / "snapshots" / "aws.json"
# A snapshot whose resource carries evidence, for the tools that need an evidence ID.
OWNED = README_SUITE.parent / "ownership" / "snapshot.json"


def test_a_client_sees_the_server_name_instructions_and_tools() -> None:
    async def connect() -> tuple[types.Implementation, str | None, list[types.Tool]]:
        async with Client(build_server(ToolSession.from_paths(SNAPSHOT))) as client:
            return client.server_info, client.instructions, (await client.list_tools()).tools

    info, instructions, tools = asyncio.run(connect())
    assert info.name == SERVER_NAME
    assert info.version == version("archdiscov")
    assert instructions == MCP_INSTRUCTIONS
    # The registry tools are listed, then `submit_answer`.
    submit = submit_answer_declaration(MCP_SUBMIT_DESCRIPTION)
    declarations = (*default_registry().declarations(), submit)
    assert [tool.name for tool in tools] == [item.name for item in declarations]
    assert tools[-1].name == "submit_answer"
    for tool, declaration in zip(tools, declarations, strict=True):
        assert tool.description == declaration.description
        # Compared as canonical JSON: key order does not change what a client validates.
        assert json.dumps(tool.input_schema, sort_keys=True) == json.dumps(
            dict(declaration.input_schema), sort_keys=True
        )
        assert tool.output_schema is None
    # The proposal schema is `ask`'s; only the description differs.
    assert submit.input_schema == submit_answer_declaration().input_schema
    assert "exactly once" not in tools[-1].description
    # Only `submit_answer` declares a larger result, so an accepted answer stays inline.
    assert tools[-1].meta == ANSWER_RESULT_META
    assert all(tool.meta is None for tool in tools[:-1])


def test_each_connection_gets_its_own_record_over_shared_data() -> None:
    session = ToolSession.from_paths(SNAPSHOT)
    server = build_server(session)

    async def two_connections() -> tuple[Connection, Connection]:
        async with server.lifespan(server) as first, server.lifespan(server) as second:
            return first, second

    first, second = asyncio.run(two_connections())
    assert first.calls == second.calls == 0
    assert first.session.index is second.session.index is session.index
    resource = session.index.resource_ids[0]
    first.session.record.record(session.index.snapshot_id, IdKind.RESOURCE, (resource,))
    assert first.session.record.returned() != second.session.record.returned()
    assert second.session.record.returned() == session.record.returned()


# ---- dispatching calls -------------------------------------------------------------


@pytest.fixture
def connections(monkeypatch) -> list[ToolSession]:
    """Each connection's session copy, as the server makes it."""
    made: list[ToolSession] = []
    fresh = ToolSession.fresh

    def recording(session: ToolSession) -> ToolSession:
        made.append(fresh(session))
        return made[-1]

    monkeypatch.setattr(ToolSession, "fresh", recording)
    return made


@pytest.fixture
def dispatches(monkeypatch) -> list[tuple[str, str]]:
    """Each registry dispatch's start and end, with its call ID."""
    events: list[tuple[str, str]] = []
    dispatch = ToolRegistry.dispatch

    def recording(registry, session, call):
        events.append(("start", call.id))
        # Slow enough that calls dispatched concurrently would overlap.
        time.sleep(0.02)
        result = dispatch(registry, session, call)
        events.append(("end", call.id))
        return result

    monkeypatch.setattr(ToolRegistry, "dispatch", recording)
    return events


def _comparable(results: list[tuple[bool, str]]) -> list[tuple[bool, dict]]:
    """Results as data, without an unsaved analysis's creation time, which differs per run."""
    data = [(is_error, json.loads(text)) for is_error, text in results]
    for _, output in data:
        output.pop("created_at", None)
    return data


def _results(
    session: ToolSession, *connections: list[Call], limits: ServerLimits | None = None
) -> list[tuple[bool, str]]:
    """Each call's error flag and text, through one client connection per list of calls, the
    connections one after another, over one server."""
    server = build_server(session, limits)

    async def run() -> list[tuple[bool, str]]:
        results = []
        for calls in connections:
            async with Client(server) as client:
                results += [await client.call_tool(name, arguments) for name, arguments in calls]
        return [(result.is_error, result.content[0].text) for result in results]

    return asyncio.run(run())


def _direct(session: ToolSession, calls: list[Call]) -> tuple[list[tuple[bool, str]], Any]:
    """The same calls dispatched straight through the registry, and their returned IDs."""
    fresh, registry = session.fresh(), default_registry()
    results = []
    for number, (name, arguments) in enumerate(calls):
        call = ToolCall(id=f"direct-{number}", name=name, arguments=Parsed(value=arguments or {}))
        result = registry.dispatch(fresh, call)
        results.append((result.is_error, result.content))
    return results, fresh.record.returned()


def test_every_tool_is_dispatched_as_directly(connections) -> None:
    session = ToolSession.from_paths(OWNED)
    resource = session.index.resource_ids[0]
    arguments = {
        "inspect_resource": {"resource_id": resource},
        "get_neighbours": {"resource_id": resource},
        "find_paths": {"resource_id": resource},
        "get_evidence": {"evidence_id": session.index.resource_evidence_ids(resource)[0]},
        "run_analyzer": {"kind": "ownership"},
        "get_saved_analysis": {"number": 1},
        "get_saved_comparison": {"number": 1},
    }
    calls: list[Call] = [(tool.name, arguments.get(tool.name)) for tool in ALL_TOOLS]
    served = _results(session, calls)
    expected, returned = _direct(session, calls)
    assert _comparable(served) == _comparable(expected)
    assert connections[0].record.returned() == returned


def _code(result: tuple[bool, str]) -> str:
    is_error, text = result
    assert is_error, text
    return json.loads(text)["code"]


def test_failed_calls_are_error_results_with_their_codes() -> None:
    # The registry's own errors come back as in a direct dispatch; these are the server's.
    oversized = {"name": "x" * (MAX_TOOL_ARGUMENT_BYTES + 1)}
    results = _results(
        ToolSession.from_paths(SNAPSHOT),
        [
            ("not a tool name!", {}),
            ("search_resources", oversized),
            ("inspect_coverage", None),
        ],
    )
    assert [_code(result) for result in results[:-1]] == [
        "tool_unknown",
        "tool_malformed_arguments",
    ]
    # A call without arguments is a call with none.
    assert results[-1][0] is False


def test_concurrent_calls_are_dispatched_one_at_a_time_in_order(dispatches) -> None:
    session = ToolSession.from_paths(SNAPSHOT)

    async def run() -> None:
        async with Client(build_server(session)) as client:
            calls = [client.call_tool("search_resources", {"limit": n}) for n in range(1, 9)]
            await asyncio.gather(*calls)

    asyncio.run(run())
    ids = [f"mcp-{number}" for number in range(1, 9)]
    assert dispatches == [(kind, call_id) for call_id in ids for kind in ("start", "end")]


def test_an_unexpected_failure_is_an_internal_error_without_its_message(monkeypatch) -> None:
    def failing(arguments):
        raise RuntimeError("DETAIL-that-must-not-leak")

    monkeypatch.setattr(mcp_server, "parse_tool_arguments", failing)
    [result] = _results(ToolSession.from_paths(SNAPSHOT), [("search_resources", {})])
    assert _code(result) == "tool_internal_error"
    assert "DETAIL" not in result[1]


# ---- bounding a session ------------------------------------------------------------


def test_the_call_cap_is_reached_exactly_and_dispatches_nothing_past_it(
    connections, dispatches
) -> None:
    session = ToolSession.from_paths(SNAPSHOT)
    calls: list[Call] = [("search_resources", {"limit": 1})] * 5
    results = _results(session, calls, calls[:1], limits=ServerLimits(max_tool_calls=3))
    first, second = connections
    assert [is_error for is_error, _ in results[:3]] == [False] * 3
    assert [_code(result) for result in results[3:5]] == ["tool_session_limit_reached"] * 2
    # Past the cap nothing is dispatched or recorded; another connection has its full count.
    started = [call_id for kind, call_id in dispatches if kind == "start"]
    assert started == ["mcp-1", "mcp-2", "mcp-3", "mcp-1"]
    expected, returned = _direct(session, calls[:3])
    assert first.record.returned() == returned
    assert results[5] == expected[0]
    assert second.record.returned() == returned


def test_the_default_cap_leaves_room_for_many_questions() -> None:
    """A connection could answer every README example four times over, submissions
    included (one `submit_answer` per question)."""
    suite = json.loads(README_SUITE.read_text(encoding="utf-8"))
    calls = sum(
        len(json.loads((README_SUITE.parent / case["fake_transcript"]).read_text())["turns"])
        for case in suite["cases"]
    )
    assert 4 * calls <= ServerLimits().max_tool_calls


def test_per_call_limits_truncate_as_in_ask() -> None:
    session = ToolSession.from_paths(SNAPSHOT, limits=ToolLimits(result_limit=1))
    calls: list[Call] = [("search_resources", {"limit": 25})]
    [served] = _results(session, calls)
    [expected], _ = _direct(session, calls)
    assert served == expected
    assert json.loads(served[1])["resources"]["truncation"]["reasons"] == ["result_limit"]


# ---- submitting answers ------------------------------------------------------------

OWNERSHIP_CASE = README_SUITE.parent / "ownership"


def _submission(texts: list[str]) -> dict:
    [text] = texts
    data = json.loads(text)
    assert data["next"] == SUBMIT_FOLLOW_UP
    return data


def test_submissions_return_their_reasons_until_one_is_accepted_with_the_answer() -> None:
    session, calls, proposal = readme_example(OWNERSHIP_CASE)
    oversized = {**proposal, "summary": "x" * MAX_TOOL_ARGUMENT_BYTES}
    invalid = {key: value for key, value in proposal.items() if key != "contexts"}
    fabricated = {
        **proposal,
        "conclusions": [
            {"kind": "analyzer_finding", "citations": [{"finding_ids": ["finding-fabricated"]}]}
        ],
        "limitations": [],
    }
    submissions = [oversized, invalid, fabricated, proposal]
    replies = mcp_replies(session, [*calls, *(("submit_answer", item) for item in submissions)])
    *refused, (is_error, texts) = replies[len(calls) :]
    assert all(error for error, _ in refused)
    assert [_submission(texts)["error"] for _, texts in refused] == [
        "proposal_malformed",
        "proposal_invalid",
        "answer_rejected",
    ]
    assert not is_error
    present, readable, data = texts
    assert present == PRESENT_ANSWER
    assert readable.splitlines()[0] == "Answer: answered, exit code 0"
    answer = json.loads(data)
    assert answer["answer"]["outcome"] == {
        "status": "answered",
        "failure": None,
        "rejections": [],
        "malformed": None,
        "provider_error": None,
        "limit": None,
    }
    assert answer["answer"]["status"] == "supported" and answer["answer"]["conclusions"]


def test_submissions_count_toward_the_cap_but_do_not_change_the_run_record(connections) -> None:
    session, calls, proposal = readme_example(OWNERSHIP_CASE)
    limits = ServerLimits(max_tool_calls=len(calls) + 1)
    submissions: list[Call] = [("submit_answer", proposal)] * 2
    results = _results(session, calls, [*calls, *submissions], limits=limits)
    assert results[2 * len(calls)][0] is False
    assert _code(results[-1]) == "tool_session_limit_reached"
    called, submitted = connections
    assert called.record.returned() == submitted.record.returned()


# ---- prompts -----------------------------------------------------------------------


def test_the_readme_questions_are_the_only_prompts_and_return_only_their_question() -> None:
    echoed = "ECHOED-input-0123"
    unknown = [(echoed, None), ("ownership", {"resource": echoed})]

    async def run() -> tuple[list[types.Prompt], list]:
        async with Client(build_server(ToolSession.from_paths(SNAPSHOT))) as client:
            listed = (await client.list_prompts()).prompts
            results = [await client.get_prompt(kind.value) for kind in README_QUESTIONS]
            for name, arguments in unknown:
                with pytest.raises(MCPError) as raised:
                    await client.get_prompt(name, arguments)
                results.append(raised.value)
        return listed, results

    listed, results = asyncio.run(run())
    assert [prompt.name for prompt in listed] == [kind.value for kind in README_QUESTIONS]
    for prompt in listed:
        assert not prompt.arguments
        assert prompt.description == (
            f"The README example question the {prompt.name} analyzer answers."
        )
    *returned, unknown_name, unknown_arguments = results
    for result, question in zip(returned, README_QUESTIONS.values(), strict=True):
        [message] = result.messages
        assert message.role == "user"
        assert message.content == types.TextContent(type="text", text=question)
        assert result.description is None
    # An unknown prompt or arguments are a fixed error that does not echo the input.
    for error in (unknown_name, unknown_arguments):
        assert (error.code, error.message) == (types.INVALID_PARAMS, UNKNOWN_PROMPT)
        assert echoed not in str(error.error)
