import json
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from archdiscov.assistant.answers import AnswerProposal
from archdiscov.assistant.config import FakeProviderConfig
from archdiscov.assistant.conversation import (
    AssistantMessage,
    MalformedArgumentsReason,
    MalformedToolArguments,
    ParsedToolArguments,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from archdiscov.assistant.provider import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderErrorCode,
    RequestLimits,
    StopReason,
)
from archdiscov.assistant.redaction import REDACTED
from archdiscov.assistant.runs import (
    MAX_TRANSCRIPT_TEXT_CHARS,
    Answered,
    MalformedOutput,
    MalformedOutputReason,
    Proposed,
    ProviderFailed,
    RunLimits,
    RunOutcome,
    RunTranscript,
    TranscriptText,
    TranscriptTurn,
    provider_outcome,
    read_proposal,
    total_usage,
    transcript_json,
    transcript_outcome,
    transcript_text,
    transcript_tool_call,
)
from archdiscov.models.sanitization import REDACTED_MARKER, is_sensitive_path
from tests.support.run_outcomes import PROPOSAL, every_run_outcome

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
OUTCOMES = TypeAdapter(RunOutcome)
ANSWERED = Answered(message=AssistantMessage(text="Done."), stop_reason=StopReason.END)


# ---- limits ------------------------------------------------------------------------


def test_default_limits_are_conservative_and_limits_round_trip_frozen() -> None:
    assert RunLimits().model_dump() == {
        "max_provider_requests": 8,
        "max_tool_calls_per_turn": 1,
        "max_tool_calls": 32,
        "max_output_tokens": 4_096,
        "max_total_output_chars": 200_000,
        "request_timeout_seconds": 60.0,
        "overall_timeout_seconds": 300.0,
    }
    limits = RunLimits(max_tool_calls_per_turn=4, request_timeout_seconds=30)
    assert RunLimits.model_validate_json(limits.model_dump_json()) == limits
    with pytest.raises(ValidationError):
        limits.max_tool_calls = 1


@pytest.mark.parametrize(
    "field,low,high",
    [
        ("max_provider_requests", 0, 51),
        ("max_tool_calls_per_turn", 0, 33),
        ("max_tool_calls", 0, 201),
        ("max_output_tokens", 0, 128_001),
        ("max_total_output_chars", 999, 1_000_001),
        ("request_timeout_seconds", 0, 601),
        ("overall_timeout_seconds", 0, 3_601),
    ],
)
def test_out_of_range_limits_are_rejected(field: str, low: float, high: float) -> None:
    for value in (low, high):
        with pytest.raises(ValidationError):
            RunLimits.model_validate({field: value})


@pytest.mark.parametrize(
    "data,code",
    [
        ({"max_tool_calls_per_turn": 8, "max_tool_calls": 4}, "run_limits_order"),
        ({"request_timeout_seconds": 120, "overall_timeout_seconds": 60}, "run_limits_order"),
        ({"max_tool_calls": True}, "int_type"),
    ],
    ids=["calls-order", "timeout-order", "bool-is-not-int"],
)
def test_invalid_limits_are_rejected(data: dict, code: str) -> None:
    with pytest.raises(ValidationError) as raised:
        RunLimits.model_validate(data)
    assert code in {error["type"] for error in raised.value.errors()}


def test_limits_come_from_provider_configuration() -> None:
    config = FakeProviderConfig(
        max_requests=5,
        max_tool_calls=10,
        max_tool_calls_per_turn=3,
        max_output_tokens=1_000,
        request_timeout_seconds=20,
        max_latency_seconds=100,
    )
    assert RunLimits.from_config(config) == RunLimits(
        max_provider_requests=5,
        max_tool_calls=10,
        max_tool_calls_per_turn=3,
        max_output_tokens=1_000,
        request_timeout_seconds=20.0,
        overall_timeout_seconds=100.0,
    )
    assert RunLimits.from_config(config, max_total_output_chars=5_000).max_total_output_chars == (
        5_000
    )
    assert RunLimits.from_config(FakeProviderConfig()) == RunLimits()


def test_request_limits_follow_the_run_limits() -> None:
    limits = RunLimits(max_tool_calls_per_turn=2, max_output_tokens=500)
    assert limits.request_limits() == RequestLimits(
        max_output_tokens=500, timeout_seconds=60.0, max_tool_calls_per_turn=2
    )
    assert limits.request_limits(timeout_seconds=7.5).timeout_seconds == 7.5
    assert limits.request_limits(timeout_seconds=900).timeout_seconds == 60.0


# ---- outcomes ----------------------------------------------------------------------


def test_every_outcome_kind_round_trips() -> None:
    outcomes = every_run_outcome()
    kinds = {member.model_fields["kind"].default for member in get_args(get_args(RunOutcome)[0])}
    assert {outcome.kind for outcome in outcomes} == kinds
    for outcome in outcomes:
        data = json.loads(outcome.model_dump_json())
        assert OUTCOMES.validate_python(data) == outcome


ANSWERED_DATA = ANSWERED.model_dump(mode="json")
CALL_DATA = {"id": "c", "name": "t", "arguments": {"kind": "parsed", "value": {}}}
INVALID_OUTCOMES = {
    "answer-with-calls": (
        {**ANSWERED_DATA, "message": {"role": "assistant", "tool_calls": [CALL_DATA]}},
        "run_answer_not_final",
    ),
    "answer-stop-reason": ({**ANSWERED_DATA, "stop_reason": "tool_use"}, "literal_error"),
    "malformed-as-provider-error": (
        {
            "kind": "provider_error",
            "code": "provider_malformed_output",
            "retryable": False,
            "message": "m",
        },
        "run_outcome_kind_mismatch",
    ),
    "unknown-kind": ({"kind": "succeeded"}, "union_tag_invalid"),
}


@pytest.mark.parametrize("data,code", list(INVALID_OUTCOMES.values()), ids=list(INVALID_OUTCOMES))
def test_invalid_outcomes_are_rejected(data: dict, code: str) -> None:
    with pytest.raises(ValidationError) as raised:
        OUTCOMES.validate_python(data)
    assert code in {error["type"] for error in raised.value.errors()}


def _errors() -> list[type[ProviderError]]:
    pending, found = list(ProviderError.__subclasses__()), []
    while pending:
        error = pending.pop()
        found.append(error)
        pending.extend(error.__subclasses__())
    return [error for error in found if "code" in vars(error)]


@pytest.mark.parametrize("error_type", _errors(), ids=lambda error: error.code.value)
def test_provider_errors_map_to_typed_outcomes(error_type: type[ProviderError]) -> None:
    error = (
        error_type("tool_calling")
        if error_type.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
        else error_type()
    )
    outcome = provider_outcome(error)
    if error.code == ProviderErrorCode.MALFORMED_OUTPUT:
        assert outcome == MalformedOutput(reason=MalformedOutputReason.PROVIDER_REPORTED)
    else:
        assert outcome == ProviderFailed(
            code=error.code, retryable=error.retryable, message=error.message
        )


# ---- transcript text ---------------------------------------------------------------


def test_transcript_text_and_json_never_hold_a_secret() -> None:
    assert transcript_text(f"The key is {SECRET}.") == TranscriptText(
        text=f"The key is {REDACTED}.", truncated=False, chars=len(SECRET) + 12
    )
    text = "Found 3 security groups in vpc-0abc (eu-west-1); model claude-sonnet-5-5."
    assert transcript_text(text) == TranscriptText(text=text, truncated=False, chars=len(text))
    value = transcript_json({"password": "hunter2", "query": f"use {SECRET}", "limit": 5})
    data = json.loads(value.text)
    assert data == {"limit": 5, "password": REDACTED_MARKER, "query": f"use {REDACTED}"}
    with pytest.raises(ValidationError) as raised:
        TranscriptText(text=SECRET, truncated=False, chars=len(SECRET))
    assert raised.value.errors()[0]["type"] == "run_transcript_secret"


def test_long_text_is_truncated_after_redaction() -> None:
    text = f"{'x' * (MAX_TRANSCRIPT_TEXT_CHARS - 10)} {SECRET} tail"
    value = transcript_text(text)
    assert value.truncated is True
    assert len(value.text) <= MAX_TRANSCRIPT_TEXT_CHARS
    assert SECRET[:10] not in value.text
    exact = transcript_text("y" * MAX_TRANSCRIPT_TEXT_CHARS)
    assert exact.truncated is False


# ---- tool calls, turns, and transcripts --------------------------------------------


def _call(arguments=None) -> ToolCall:
    return ToolCall(
        id="call_1",
        name="search_resources",
        arguments=arguments or ParsedToolArguments(value={"name": "web", "api_token": SECRET}),
    )


def test_tool_calls_record_sanitized_arguments_and_bounded_results() -> None:
    result = ToolResult(call_id="call_1", content=f"{SECRET} " + "r" * 10_000)
    recorded = transcript_tool_call(_call(), result)
    assert json.loads(recorded.arguments.text) == {"api_token": REDACTED_MARKER, "name": "web"}
    assert recorded.result.truncated is True
    assert recorded.result.text.startswith(REDACTED)
    assert recorded.result_is_error is False
    malformed = transcript_tool_call(
        _call(MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON)),
        ToolResult(call_id="call_1", content="{}", is_error=True),
    )
    assert malformed.arguments is None
    assert malformed.malformed_arguments == MalformedArgumentsReason.INVALID_JSON
    with pytest.raises(ValueError):
        transcript_tool_call(_call(), ToolResult(call_id="other", content=""))


def _turn(request: int, calls: int = 0, **usage) -> TranscriptTurn:
    recorded = transcript_tool_call(_call(), ToolResult(call_id="call_1", content="{}"))
    return TranscriptTurn(
        request=request,
        latency_seconds=0.5,
        stop_reason=StopReason.TOOL_USE if calls else StopReason.END,
        usage=TokenUsage(**usage),
        text=transcript_text("ok"),
        tool_calls=(recorded,) * calls,
    )


def _transcript(turns, outcome=None, limits=None, **fields) -> RunTranscript:
    return RunTranscript(
        provider_kind=fields.get("provider_kind", "fake"),
        model=fields.get("model", "fake"),
        limits=limits or RunLimits(),
        turns=tuple(turns),
        usage=fields.get("usage", total_usage(turns)),
        latency_seconds=1.0,
        outcome=outcome or ANSWERED,
    )


def test_transcripts_round_trip_and_total_their_usage() -> None:
    turns = [_turn(0, calls=1, input_tokens=10), _turn(1, input_tokens=5, output_tokens=3)]
    for outcome in every_run_outcome():
        transcript = _transcript(turns, outcome)
        assert RunTranscript.model_validate_json(transcript.model_dump_json()) == transcript
    assert _transcript(turns).usage == TokenUsage(input_tokens=15, output_tokens=3)
    assert total_usage([]) == TokenUsage()
    assert total_usage([_turn(0, output_tokens=2), _turn(1)]) == TokenUsage(output_tokens=2)


INCONSISTENT_TRANSCRIPTS = {
    "unnumbered": lambda: _transcript([_turn(1)]),
    "repair-unoffered": lambda: _transcript(
        [_turn(0), _turn(1).model_copy(update={"repair": True})]
    ),
    "requests": lambda: _transcript(
        [_turn(index) for index in range(3)], limits=RunLimits(max_provider_requests=2)
    ),
    "tool-calls": lambda: _transcript([_turn(0, calls=3)], limits=RunLimits(max_tool_calls=2)),
    "usage": lambda: _transcript([_turn(0, input_tokens=4)], usage=TokenUsage(input_tokens=5)),
    "secret-model": lambda: _transcript([], model=SECRET),
    "kind": lambda: _transcript([], provider_kind="Not A Kind"),
}


@pytest.mark.parametrize(
    "build", list(INCONSISTENT_TRANSCRIPTS.values()), ids=list(INCONSISTENT_TRANSCRIPTS)
)
def test_inconsistent_transcripts_are_rejected(build) -> None:
    with pytest.raises(ValidationError):
        build()


def _property_names(schema) -> set[str]:
    if isinstance(schema, list):
        return set().union(*map(_property_names, schema))
    if not isinstance(schema, dict):
        return set()
    return set(schema.get("properties", {})).union(*map(_property_names, schema.values()))


def test_transcripts_have_no_credential_fields_or_values() -> None:
    fields = _property_names(RunTranscript.model_json_schema())
    assert fields and not any(is_sensitive_path(name) for name in fields)
    call = _call(ParsedToolArguments(value={"name": SECRET, "note": f"Bearer {SECRET}"}))
    recorded = transcript_tool_call(call, ToolResult(call_id="call_1", content=f"echo {SECRET}"))
    turn = _turn(0, calls=1).model_copy(
        update={"text": transcript_text(f"Calling with {SECRET}"), "tool_calls": (recorded,)}
    )
    transcript = _transcript([turn], ProviderFailed.from_error(ProviderAuthenticationError()))
    assert SECRET not in transcript.model_dump_json()
    assert SECRET[:16] not in repr(transcript)


def test_transcripts_record_answers_and_proposals_redacted() -> None:
    answer = Answered(
        message=AssistantMessage(text=f"Key {SECRET} " + "z" * 5_000), stop_reason=StopReason.END
    )
    recorded = transcript_outcome(answer)
    assert SECRET not in recorded.message.text
    assert len(recorded.message.text) <= MAX_TRANSCRIPT_TEXT_CHARS
    assert recorded.stop_reason == StopReason.END
    proposal = Proposed(
        proposal=AnswerProposal.model_validate(
            {
                "status": "partial",
                "contexts": [{"side": "primary", "source_kind": "terraform_plan"}],
                "conclusions": [
                    {
                        "kind": "analyzer_finding",
                        "citations": [
                            {"finding_ids": ["f-1", SECRET], "evidence_ids": [SECRET + "x"]}
                        ],
                    }
                ],
                "limitations": [{"finding_id": SECRET, "limitation": "coverage_failed"}],
                "summary": f"Key {SECRET} was mentioned.",
                "explanation": f"Bearer {SECRET}",
            }
        )
    )
    proposed = transcript_outcome(proposal)
    assert SECRET not in proposed.model_dump_json()
    citation = proposed.proposal.conclusions[0].citations[0]
    assert citation.finding_ids[0] == "f-1"
    placeholders = [
        citation.finding_ids[1],
        citation.evidence_ids[0],
        proposed.proposal.limitations[0].finding_id,
    ]
    assert len(set(placeholders)) == 3 and all(item.startswith(REDACTED) for item in placeholders)
    assert proposed.proposal.status == proposal.proposal.status
    assert proposed.proposal.contexts == proposal.proposal.contexts
    for raw, cleaned in ((answer, recorded), (proposal, proposed)):
        assert _transcript([], cleaned).outcome == cleaned
        with pytest.raises(ValidationError) as raised:
            _transcript([], raw)
        assert raised.value.errors()[0]["type"] == "run_transcript_secret"
    for other in every_run_outcome():
        if not isinstance(other, Answered | Proposed):
            assert transcript_outcome(other) is other


def test_read_proposal_reads_a_valid_proposal_and_names_why_others_fail() -> None:
    data = PROPOSAL.model_dump(mode="json")
    assert read_proposal(ParsedToolArguments(value=data)) == Proposed(proposal=PROPOSAL)
    oversized = MalformedToolArguments(reason=MalformedArgumentsReason.TOO_LARGE)
    assert read_proposal(oversized) == MalformedOutput(
        reason=MalformedOutputReason.PROPOSAL_MALFORMED
    )
    # A proposal of the right shape whose status needs conclusions it does not have.
    invalid = ParsedToolArguments(
        value={**data, "status": "supported", "insufficient_reason": None}
    )
    assert read_proposal(invalid) == MalformedOutput(reason=MalformedOutputReason.PROPOSAL_INVALID)
