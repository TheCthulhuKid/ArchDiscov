import json

import pytest

from archdiscov.assistant.conversation import (
    AssistantMessage,
    ParsedToolArguments,
    ToolCall,
    ToolResultMessage,
)
from archdiscov.assistant.fake import FakeProvider, FakeTranscript
from archdiscov.assistant.instructions import SYSTEM_INSTRUCTIONS
from archdiscov.assistant.orchestrator import run_assistant
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
from tests.support.scripted_turns import (
    QUESTION,
    SNAPSHOT,
    calls_turn,
    fail_turn,
    run_turns,
    text_turn,
)

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
TOOL_NAMES = [declaration.name for declaration in default_registry().declarations()] + [
    "submit_answer"
]


def _first_resource() -> str:
    return ToolSession.from_paths(SNAPSHOT).index.resource_ids[0]


# ---- answers and tool use ----------------------------------------------------------


def test_direct_answer_is_returned_as_is() -> None:
    result, provider, _ = run_turns([text_turn("Nothing to report.")])
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
    result, _, _ = run_turns([text_turn("Partial", stop=stop)])
    assert isinstance(result.outcome, Answered)
    assert result.outcome.stop_reason == stop


def test_multi_step_tool_use_records_returned_ids() -> None:
    resource = _first_resource()
    result, provider, session = run_turns(
        [
            calls_turn(("c1", "search_resources", {}), expect={"tool_names": TOOL_NAMES}),
            calls_turn(
                ("c2", "inspect_resource", {"resource_id": resource}),
                expect={"last_message_role": "tool"},
            ),
            text_turn("Answer.", expect={"last_message_role": "tool"}),
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
    result, _, _ = run_turns(
        [
            calls_turn(
                ("c1", "search_resources", {}),
                ("c2", "inspect_resource", {"resource_id": resource}),
                text="Checking two things.",
            ),
            text_turn(),
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
    result, provider, _ = run_turns([calls_turn(call), text_turn("Recovered.")])
    assert isinstance(result.outcome, Answered)
    tool_message = provider.requests[1].request.conversation.messages[-1]
    assert tool_message.results[0].is_error is True
    assert json.loads(tool_message.results[0].content)["code"] == code
    recorded = result.transcript.turns[0].tool_calls[0]
    assert recorded.result_is_error is True
    assert (recorded.malformed_arguments is not None) == (call[2] is None)


# ---- limits ------------------------------------------------------------------------


def test_provider_request_limit_ends_the_run_without_running_unread_calls() -> None:
    result, _, session = run_turns(
        [calls_turn(("c1", "search_resources", {})), calls_turn(("c2", "search_resources", {}))],
        limits=RunLimits(max_provider_requests=2),
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.PROVIDER_REQUESTS)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [1, 0]
    assert result.conversation.messages[-1].tool_calls[0].id == "c2"
    assert result.returned == session.record.returned()


def test_single_request_limit_with_tool_calls() -> None:
    result, _, session = run_turns(
        [calls_turn(("c1", "search_resources", {}))], limits=RunLimits(max_provider_requests=1)
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.PROVIDER_REQUESTS)
    assert session.record.returned() == ReturnedIds()


def test_tool_calls_per_turn_limit_runs_none_of_the_excess_turn() -> None:
    result, provider, session = run_turns(
        [
            calls_turn(
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
    result, _, _ = run_turns(
        [
            calls_turn(("c1", "search_resources", {}), ("c2", "list_analyzers", {})),
            calls_turn(("c3", "inspect_coverage", {})),
        ],
        limits=RunLimits(max_tool_calls=2, max_tool_calls_per_turn=2),
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.TOOL_CALLS)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [2, 0]


def test_total_output_limit_counts_tool_results() -> None:
    result, _, session = run_turns(
        [calls_turn(("c1", "search_resources", {}))], limits=RunLimits(max_total_output_chars=1_000)
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)
    assert result.returned == session.record.returned()
    assert result.returned != ReturnedIds()


def test_total_output_limit_counts_assistant_text() -> None:
    result, _, _ = run_turns(
        [text_turn("x" * 1_001)], limits=RunLimits(max_total_output_chars=1_000)
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)


# ---- provider errors and malformed output ------------------------------------------

SCRIPTABLE = [
    code
    for code in ProviderErrorCode
    if code not in {ProviderErrorCode.UNSUPPORTED_CAPABILITY, ProviderErrorCode.MALFORMED_OUTPUT}
]


@pytest.mark.parametrize("code", SCRIPTABLE, ids=lambda code: code.value)
def test_provider_errors_end_the_run_with_typed_outcomes(code: ProviderErrorCode) -> None:
    result, _, _ = run_turns([calls_turn(("c1", "search_resources", {})), fail_turn(code.value)])
    assert isinstance(result.outcome, ProviderFailed)
    assert result.outcome.code == code
    assert [turn.error for turn in result.transcript.turns] == [None, code]
    assert result.transcript.turns[1].stop_reason is None


def test_provider_malformed_output_is_its_own_outcome() -> None:
    result, _, _ = run_turns([fail_turn("provider_malformed_output")])
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.PROVIDER_REPORTED)


def test_provider_retries_are_invisible_to_the_loop() -> None:
    result, provider, _ = run_turns([fail_turn("provider_transport"), text_turn()], max_retries=1)
    assert isinstance(result.outcome, Answered)
    assert len(provider.requests) == 2
    assert len(result.transcript.turns) == 1


def test_unsupported_capabilities_fail_before_any_attempt() -> None:
    data = {"turns": [text_turn()], "capabilities": {"capabilities": []}}
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
    result, _, _ = run_turns(
        [calls_turn(("c1", "search_resources", {})), calls_turn(("c1", "list_analyzers", {}))]
    )
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.CONVERSATION_INVALID)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [1, 0]


def test_tool_calls_without_offered_tools_are_malformed_output() -> None:
    result, provider, _ = run_turns(
        [calls_turn(("c1", "search_resources", {}))],
        registry=ToolRegistry([]),
        offer_answer_tool=False,
    )
    assert provider.requests[0].request.tools == ()
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.TOOL_CALLS_WITHOUT_TOOLS)


# ---- transcripts -------------------------------------------------------------------


def test_transcript_records_usage_latency_and_model() -> None:
    result, _, _ = run_turns(
        [
            calls_turn(("c1", "search_resources", {}), delay_seconds=1.5),
            {**text_turn(), "delay_seconds": 2.0},
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
    result, _, _ = run_turns(
        [
            calls_turn(("c1", "search_resources", {"name": SECRET}), text=f"Using {SECRET}"),
            text_turn(f"The answer mentions {SECRET}."),
        ],
        question=f"Does {SECRET} appear?",
    )
    assert SECRET not in result.transcript.model_dump_json()
    assert SECRET in result.outcome.message.text


def test_runs_are_deterministic() -> None:
    turns = [calls_turn(("c1", "search_resources", {})), text_turn()]
    first, _, _ = run_turns(turns)
    second, _, _ = run_turns(turns)
    assert first == second


def test_final_conversation_is_valid_and_ends_with_the_answer() -> None:
    result, _, _ = run_turns([calls_turn(("c1", "search_resources", {})), text_turn("Final.")])
    messages = result.conversation.messages
    assert messages[-1] == AssistantMessage(text="Final.")
    assert (
        ToolCall(id="c1", name="search_resources", arguments=ParsedToolArguments(value={}))
        in messages[2].tool_calls
    )
