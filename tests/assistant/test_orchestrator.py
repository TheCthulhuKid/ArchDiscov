import json
from typing import Any

import pytest

from archdiscov.assistant.conversation import (
    AssistantMessage,
    ParsedToolArguments,
    ToolCall,
    ToolResultMessage,
)
from archdiscov.assistant.fake import FakeClock, FakeProvider, FakeTranscript
from archdiscov.assistant.instructions import SYSTEM_INSTRUCTIONS
from archdiscov.assistant.orchestrator import RunResult, run_assistant
from archdiscov.assistant.provider import ProviderErrorCode, StopReason
from archdiscov.assistant.runs import (
    Answered,
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    ProviderFailed,
    RunLimitKind,
    RunLimits,
)
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.assistant.tools.contracts import IdKind
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.registry import ToolRegistry
from archdiscov.assistant.tools.session import ToolSession
from tests.paths import FIXTURES_ROOT

SNAPSHOT = FIXTURES_ROOT / "snapshots" / "network.json"
SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
QUESTION = "Which resources are in the network?"
TOOL_NAMES = [declaration.name for declaration in default_registry().declarations()] + [
    "submit_answer"
]


def _text(text: str = "Done.", stop: str = "end", **turn: Any) -> dict:
    return {
        "kind": "respond",
        "response": {"message": {"role": "assistant", "text": text}, "stop_reason": stop},
        **turn,
    }


def _calls(*calls: tuple[str, str, Any], text: str | None = None, **turn: Any) -> dict:
    """Tool calls as (id, name, arguments); arguments of None are malformed."""
    message: dict[str, Any] = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "name": name,
                "arguments": {"kind": "malformed", "reason": "invalid_json"}
                if arguments is None
                else {"kind": "parsed", "value": arguments},
            }
            for call_id, name, arguments in calls
        ],
    }
    if text is not None:
        message["text"] = text
    return {
        "kind": "respond",
        "response": {"message": message, "stop_reason": "tool_use", "usage": {"output_tokens": 5}},
        **turn,
    }


def _continued(turn: dict, provider_kind: str = "fake", *blocks: dict) -> dict:
    """The scripted turn with a continuation on its message; one thinking block by default."""
    blocks = blocks or ({"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl"},)
    message = {**turn["response"]["message"]}
    message["continuation"] = {"provider_kind": provider_kind, "blocks": list(blocks)}
    return {**turn, "response": {**turn["response"], "message": message}}


def _fail(code: str) -> dict:
    return {"kind": "fail", "error": code}


def _run(
    turns: list[dict],
    *,
    limits: RunLimits | None = None,
    registry: ToolRegistry | None = None,
    offer_answer_tool: bool = True,
    capabilities: list[str] | None = None,
    max_retries: int = 0,
    question: str = QUESTION,
) -> tuple[RunResult, FakeProvider, ToolSession]:
    data: dict[str, Any] = {"turns": turns}
    if capabilities is not None:
        data["capabilities"] = {"capabilities": capabilities}
    clock = FakeClock()
    provider = FakeProvider(
        FakeTranscript.model_validate(data), clock=clock, max_retries=max_retries
    )
    session = ToolSession.from_paths(SNAPSHOT)
    result = run_assistant(
        question,
        provider=provider,
        session=session,
        provider_kind="fake",
        model="fake",
        limits=limits,
        registry=registry,
        clock=clock.monotonic,
        offer_answer_tool=offer_answer_tool,
    )
    assert provider.remaining_turns == 0
    return result, provider, session


def _first_resource() -> str:
    return ToolSession.from_paths(SNAPSHOT).index.resource_ids[0]


# ---- answers and tool use ----------------------------------------------------------


def test_direct_answer_is_returned_as_is() -> None:
    result, provider, _ = _run([_text("Nothing to report.")])
    assert result.outcome == Answered(
        message=AssistantMessage(text="Nothing to report."), stop_reason=StopReason.END
    )
    request = provider.requests[0].request
    assert [message.role for message in request.conversation.messages] == ["system", "user"]
    assert request.conversation.messages[0].text == SYSTEM_INSTRUCTIONS
    assert request.conversation.messages[1].text == QUESTION
    assert [tool.name for tool in request.tools] == TOOL_NAMES
    assert result.returned == ReturnedIds()
    assert len(result.transcript.turns) == 1
    assert result.conversation.messages[-1] == result.outcome.message


@pytest.mark.parametrize("stop", ["length", "refusal"])
def test_truncated_and_refused_answers_are_still_answers(stop: str) -> None:
    result, _, _ = _run([_text("Partial", stop=stop)])
    assert isinstance(result.outcome, Answered)
    assert result.outcome.stop_reason == stop


def test_multi_step_tool_use_records_returned_ids() -> None:
    resource = _first_resource()
    result, provider, session = _run(
        [
            _calls(("c1", "search_resources", {}), expect={"tool_names": TOOL_NAMES}),
            _calls(
                ("c2", "inspect_resource", {"resource_id": resource}),
                expect={"last_message_role": "tool"},
            ),
            _text("Answer.", expect={"last_message_role": "tool"}),
        ]
    )
    assert isinstance(result.outcome, Answered)
    assert result.returned == session.record.returned()
    assert result.returned.contains(IdKind.RESOURCE, session.index.snapshot_id, resource)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [1, 1, 0]
    roles = [message.role for message in result.conversation.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"]
    third = provider.requests[2].request.conversation.messages
    assert isinstance(third[-1], ToolResultMessage)
    assert third[-1].results[0].call_id == "c2"


def test_parallel_tool_calls_run_in_one_turn() -> None:
    resource = _first_resource()
    result, _, _ = _run(
        [
            _calls(
                ("c1", "search_resources", {}),
                ("c2", "inspect_resource", {"resource_id": resource}),
                text="Checking two things.",
            ),
            _text(),
        ],
        limits=RunLimits(max_tool_calls_per_turn=4),
    )
    assert isinstance(result.outcome, Answered)
    results = result.conversation.messages[3]
    assert isinstance(results, ToolResultMessage)
    assert [item.call_id for item in results.results] == ["c1", "c2"]
    turn = result.transcript.turns[0]
    assert [call.call_id for call in turn.tool_calls] == ["c1", "c2"]
    assert turn.text.text == "Checking two things."


@pytest.mark.parametrize(
    "call,code",
    [
        (("c1", "delete_everything", {}), "tool_unknown"),
        (("c1", "inspect_resource", None), "tool_malformed_arguments"),
        (
            ("c1", "inspect_resource", {"resource_id": "x", "path": "/etc"}),
            "tool_invalid_arguments",
        ),
        (("c1", "inspect_resource", {"resource_id": "missing"}), "tool_not_found"),
    ],
)
def test_tool_mistakes_are_answered_to_the_model(call: tuple, code: str) -> None:
    result, provider, _ = _run([_calls(call), _text("Recovered.")])
    assert isinstance(result.outcome, Answered)
    tool_message = provider.requests[1].request.conversation.messages[-1]
    assert tool_message.results[0].is_error is True
    assert json.loads(tool_message.results[0].content)["code"] == code
    recorded = result.transcript.turns[0].tool_calls[0]
    assert recorded.result_is_error is True
    assert (recorded.malformed_arguments is not None) == (call[2] is None)


# ---- limits ------------------------------------------------------------------------


def test_provider_request_limit_ends_the_run_without_running_unread_calls() -> None:
    result, _, session = _run(
        [_calls(("c1", "search_resources", {})), _calls(("c2", "search_resources", {}))],
        limits=RunLimits(max_provider_requests=2),
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.PROVIDER_REQUESTS)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [1, 0]
    assert result.conversation.messages[-1].tool_calls[0].id == "c2"
    assert result.returned == session.record.returned()


def test_single_request_limit_with_tool_calls() -> None:
    result, _, session = _run(
        [_calls(("c1", "search_resources", {}))], limits=RunLimits(max_provider_requests=1)
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.PROVIDER_REQUESTS)
    assert session.record.returned() == ReturnedIds()


def test_tool_calls_per_turn_limit_runs_none_of_the_excess_turn() -> None:
    result, provider, session = _run(
        [
            _calls(
                ("c1", "search_resources", {}),
                ("c2", "search_resources", {}),
                ignore_request_limits=True,
            )
        ]
    )
    assert provider.requests[0].request.limits.max_tool_calls_per_turn == 1
    assert result.outcome == LimitReached(limit=RunLimitKind.TOOL_CALLS_PER_TURN)
    assert result.transcript.turns[0].tool_calls == ()
    assert session.record.returned() == ReturnedIds()


def test_tool_call_limit_counts_across_turns() -> None:
    result, _, _ = _run(
        [
            _calls(("c1", "search_resources", {}), ("c2", "list_analyzers", {})),
            _calls(("c3", "inspect_coverage", {})),
        ],
        limits=RunLimits(max_tool_calls=2, max_tool_calls_per_turn=2),
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.TOOL_CALLS)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [2, 0]


def test_total_output_limit_counts_tool_results() -> None:
    result, _, session = _run(
        [_calls(("c1", "search_resources", {}))], limits=RunLimits(max_total_output_chars=1_000)
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)
    assert result.returned == session.record.returned()
    assert result.returned != ReturnedIds()


def test_total_output_limit_counts_assistant_text() -> None:
    result, _, _ = _run([_text("x" * 1_001)], limits=RunLimits(max_total_output_chars=1_000))
    assert result.outcome == LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)


# ---- provider errors and malformed output ------------------------------------------

SCRIPTABLE = [
    code
    for code in ProviderErrorCode
    if code not in {ProviderErrorCode.UNSUPPORTED_CAPABILITY, ProviderErrorCode.MALFORMED_OUTPUT}
]


@pytest.mark.parametrize("code", SCRIPTABLE, ids=lambda code: code.value)
def test_provider_errors_end_the_run_with_typed_outcomes(code: ProviderErrorCode) -> None:
    result, _, _ = _run([_calls(("c1", "search_resources", {})), _fail(code.value)])
    assert isinstance(result.outcome, ProviderFailed)
    assert result.outcome.code == code
    assert [turn.error for turn in result.transcript.turns] == [None, code]
    assert result.transcript.turns[1].stop_reason is None


def test_provider_malformed_output_is_its_own_outcome() -> None:
    result, _, _ = _run([_fail("provider_malformed_output")])
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.PROVIDER_REPORTED)


def test_provider_retries_are_invisible_to_the_loop() -> None:
    result, provider, _ = _run([_fail("provider_transport"), _text()], max_retries=1)
    assert isinstance(result.outcome, Answered)
    assert len(provider.requests) == 2
    assert len(result.transcript.turns) == 1


def test_unsupported_capabilities_fail_before_any_attempt() -> None:
    data = {"turns": [_text()], "capabilities": {"capabilities": []}}
    provider = FakeProvider(FakeTranscript.model_validate(data))
    result = run_assistant(
        QUESTION,
        provider=provider,
        session=ToolSession.from_paths(SNAPSHOT),
        provider_kind="fake",
        model="fake",
    )
    assert result.outcome.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert provider.requests == ()


def test_reused_call_ids_are_malformed_output() -> None:
    result, _, _ = _run(
        [_calls(("c1", "search_resources", {})), _calls(("c1", "list_analyzers", {}))]
    )
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.CONVERSATION_INVALID)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [1, 0]


def test_tool_calls_without_offered_tools_are_malformed_output() -> None:
    result, provider, _ = _run(
        [_calls(("c1", "search_resources", {}))], registry=ToolRegistry([]), offer_answer_tool=False
    )
    assert provider.requests[0].request.tools == ()
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.TOOL_CALLS_WITHOUT_TOOLS)


# ---- transcripts -------------------------------------------------------------------


def test_transcript_records_usage_latency_and_model() -> None:
    result, _, _ = _run(
        [
            _calls(("c1", "search_resources", {}), delay_seconds=1.5),
            {**_text(), "delay_seconds": 2.0},
        ]
    )
    transcript = result.transcript
    assert (transcript.provider_kind, transcript.model) == ("fake", "fake")
    assert [turn.latency_seconds for turn in transcript.turns] == [1.5, 2.0]
    assert transcript.latency_seconds == 3.5
    assert transcript.usage.output_tokens == 5
    assert transcript.outcome == result.outcome
    assert transcript.limits == RunLimits()


def test_transcripts_never_contain_secrets() -> None:
    result, _, _ = _run(
        [
            _calls(("c1", "search_resources", {"name": SECRET}), text=f"Using {SECRET}"),
            _text(f"The answer mentions {SECRET}."),
        ],
        question=f"Does {SECRET} appear?",
    )
    assert SECRET not in result.transcript.model_dump_json()
    assert SECRET in result.outcome.message.text


def test_runs_are_deterministic() -> None:
    turns = [_calls(("c1", "search_resources", {})), _text()]
    first, _, _ = _run(turns)
    second, _, _ = _run(turns)
    assert first == second


def test_final_conversation_is_valid_and_ends_with_the_answer() -> None:
    result, _, _ = _run([_calls(("c1", "search_resources", {})), _text("Final.")])
    messages = result.conversation.messages
    assert messages[-1] == AssistantMessage(text="Final.")
    assert (
        ToolCall(id="c1", name="search_resources", arguments=ParsedToolArguments(value={}))
        in messages[2].tool_calls
    )
