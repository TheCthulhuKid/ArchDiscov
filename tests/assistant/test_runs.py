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
    ProviderErrorCode,
    ProviderMalformedOutputError,
    ProviderRateLimitError,
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


def test_per_request_limits_cannot_exceed_the_run_limits() -> None:
    with pytest.raises(ValidationError) as raised:
        RunLimits(max_tool_calls_per_turn=8, max_tool_calls=4)
    assert raised.value.errors()[0]["type"] == "run_limits_order"


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
    "malformed-as-provider-error": (
        {
            "kind": "provider_error",
            "code": "provider_malformed_output",
            "retryable": False,
            "message": "m",
        },
        "run_outcome_kind_mismatch",
    ),
}


@pytest.mark.parametrize("data,code", list(INVALID_OUTCOMES.values()), ids=list(INVALID_OUTCOMES))
def test_invalid_outcomes_are_rejected(data: dict, code: str) -> None:
    with pytest.raises(ValidationError) as raised:
        OUTCOMES.validate_python(data)
    assert code in {error["type"] for error in raised.value.errors()}


def test_provider_errors_map_to_typed_outcomes() -> None:
    error = ProviderRateLimitError()
    assert provider_outcome(error) == ProviderFailed(
        code=ProviderErrorCode.RATE_LIMIT, retryable=True, message=error.message
    )
    # Malformed output has its own kind.
    assert provider_outcome(ProviderMalformedOutputError()) == MalformedOutput(
        reason=MalformedOutputReason.PROVIDER_REPORTED
    )


# ---- transcript text ---------------------------------------------------------------


def test_transcript_text_never_holds_a_secret() -> None:
    # Whole transcripts, tool arguments included, are in the tests below.
    assert transcript_text(f"The key is {SECRET}.") == TranscriptText(
        text=f"The key is {REDACTED}.", truncated=False, chars=len(SECRET) + 12
    )
    with pytest.raises(ValidationError) as raised:
        TranscriptText(text=SECRET, truncated=False, chars=len(SECRET))
    assert raised.value.errors()[0]["type"] == "run_transcript_secret"


def test_long_text_is_truncated_after_redaction() -> None:
    text = f"{'x' * (MAX_TRANSCRIPT_TEXT_CHARS - 10)} {SECRET} tail"
    value = transcript_text(text)
    assert value.truncated is True
    assert len(value.text) <= MAX_TRANSCRIPT_TEXT_CHARS
    assert SECRET[:10] not in value.text


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


def _transcript(turns, outcome=None, **fields) -> RunTranscript:
    return RunTranscript(
        provider_kind="fake",
        model=fields.get("model", "fake"),
        limits=RunLimits(),
        turns=tuple(turns),
        usage=fields.get("usage", total_usage(turns)),
        latency_seconds=1.0,
        outcome=outcome or ANSWERED,
    )


def test_transcripts_round_trip_and_total_their_usage() -> None:
    turns = [_turn(0, calls=1, input_tokens=10), _turn(1, input_tokens=5, output_tokens=3)]
    transcript = _transcript(turns)
    assert RunTranscript.model_validate_json(transcript.model_dump_json()) == transcript
    assert transcript.usage == TokenUsage(input_tokens=15, output_tokens=3)
    # A count no turn reported stays unknown.
    assert total_usage([_turn(0, output_tokens=2), _turn(1)]) == TokenUsage(output_tokens=2)


INCONSISTENT_TRANSCRIPTS = {
    "repair-unoffered": lambda: _transcript(
        [_turn(0), _turn(1).model_copy(update={"repair": True})]
    ),
    "usage": lambda: _transcript([_turn(0, input_tokens=4)], usage=TokenUsage(input_tokens=5)),
    "secret-model": lambda: _transcript([], model=SECRET),
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
