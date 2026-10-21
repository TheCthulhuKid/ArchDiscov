import json
import time
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

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
from archdiscov.assistant.redaction import REDACTED, looks_secret, redact_secrets
from archdiscov.assistant.runs import (
    MAX_TRANSCRIPT_TEXT_CHARS,
    Answered,
    Cancelled,
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    ProviderFailed,
    RunLimitKind,
    RunLimits,
    RunOutcome,
    RunTranscript,
    TranscriptText,
    TranscriptTurn,
    provider_outcome,
    total_usage,
    transcript_json,
    transcript_text,
    transcript_tool_call,
)
from archdiscov.models.sanitization import REDACTED_MARKER, is_sensitive_path

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
OUTCOMES = TypeAdapter(RunOutcome)


# ---- limits ------------------------------------------------------------------------


def test_limits_have_conservative_defaults() -> None:
    assert RunLimits().model_dump() == {
        "max_provider_requests": 8,
        "max_tool_calls_per_turn": 1,
        "max_tool_calls": 32,
        "max_output_tokens": 4_096,
        "max_total_output_chars": 200_000,
        "request_timeout_seconds": 60.0,
        "overall_timeout_seconds": 300.0,
    }


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
        ({"max_provider_requests": "8"}, "int_type"),
        ({"max_tool_calls": True}, "int_type"),
        ({"max_retries": 2}, "extra_forbidden"),
    ],
)
def test_invalid_limits_are_rejected(data: dict, code: str) -> None:
    with pytest.raises(ValidationError) as raised:
        RunLimits.model_validate(data)
    assert code in {error["type"] for error in raised.value.errors()}


def test_limits_round_trip_and_are_frozen() -> None:
    limits = RunLimits(max_tool_calls_per_turn=4, request_timeout_seconds=30)
    assert RunLimits.model_validate_json(limits.model_dump_json()) == limits
    with pytest.raises(ValidationError):
        limits.max_tool_calls = 1


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

EXAMPLES = {
    "answered": Answered(message=AssistantMessage(text="Done."), stop_reason=StopReason.END),
    "limit_reached": LimitReached(limit=RunLimitKind.TOOL_CALLS),
    "provider_error": ProviderFailed(
        code=ProviderErrorCode.TIMEOUT, retryable=False, message="Provider request timed out."
    ),
    "malformed_output": MalformedOutput(reason=MalformedOutputReason.CONVERSATION_INVALID),
    "cancelled": Cancelled(),
}


def test_every_outcome_kind_has_an_example() -> None:
    kinds = {member.model_fields["kind"].default for member in get_args(get_args(RunOutcome)[0])}
    assert kinds == set(EXAMPLES)


@pytest.mark.parametrize("kind", list(EXAMPLES))
def test_outcomes_round_trip(kind: str) -> None:
    outcome = EXAMPLES[kind]
    data = json.loads(outcome.model_dump_json())
    assert data["kind"] == kind
    assert OUTCOMES.validate_python(data) == outcome


@pytest.mark.parametrize("limit", list(RunLimitKind))
def test_limit_reached_names_each_limit(limit: RunLimitKind) -> None:
    assert OUTCOMES.validate_python({"kind": "limit_reached", "limit": limit.value}).limit == limit


@pytest.mark.parametrize(
    "data,code",
    [
        (
            {
                "kind": "answered",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "c", "name": "t", "arguments": {"kind": "parsed", "value": {}}}
                    ],
                },
                "stop_reason": "end",
            },
            "run_answer_not_final",
        ),
        (
            {
                "kind": "answered",
                "message": {"role": "assistant", "text": "x"},
                "stop_reason": "tool_use",
            },
            "literal_error",
        ),
        (
            {
                "kind": "provider_error",
                "code": "provider_malformed_output",
                "retryable": False,
                "message": "m",
            },
            "run_outcome_kind_mismatch",
        ),
        ({"kind": "limit_reached", "limit": "patience"}, "enum"),
        ({"kind": "succeeded"}, "union_tag_invalid"),
        ({"kind": "cancelled", "reason": "user"}, "extra_forbidden"),
    ],
)
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


@pytest.mark.parametrize(
    "text",
    [
        f"The key is {SECRET}.",
        "AKIAIOSFODNN7EXAMPLE was used",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY----- after",
        "token aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z end",
    ],
)
def test_secret_looking_text_is_redacted(text: str) -> None:
    value = transcript_text(text)
    assert REDACTED in value.text
    assert not looks_secret(value.text)
    assert value.chars == len(text)


def test_ordinary_text_is_kept() -> None:
    text = "Found 3 security groups in vpc-0abc (eu-west-1); model claude-sonnet-5-5."
    assert transcript_text(text) == TranscriptText(text=text, truncated=False, chars=len(text))


def test_long_text_is_truncated_after_redaction() -> None:
    text = f"{'x' * (MAX_TRANSCRIPT_TEXT_CHARS - 10)} {SECRET} tail"
    value = transcript_text(text)
    assert value.truncated is True
    assert len(value.text) <= MAX_TRANSCRIPT_TEXT_CHARS
    assert SECRET[:10] not in value.text
    exact = transcript_text("y" * MAX_TRANSCRIPT_TEXT_CHARS)
    assert exact.truncated is False


def test_redaction_is_fast_on_large_inputs() -> None:
    started = time.monotonic()
    transcript_text("aB3" * 100_000)
    redact_secrets("aB3" * 20_000 + " " + "x" * 200_000)
    assert time.monotonic() - started < 2


def test_transcript_text_rejects_secrets_built_directly() -> None:
    with pytest.raises(ValidationError) as raised:
        TranscriptText(text=SECRET, truncated=False, chars=len(SECRET))
    assert raised.value.errors()[0]["type"] == "run_transcript_secret"


def test_json_values_are_sanitized_and_redacted() -> None:
    value = transcript_json({"password": "hunter2", "query": f"use {SECRET}", "limit": 5})
    data = json.loads(value.text)
    assert data == {"limit": 5, "password": REDACTED_MARKER, "query": f"use {REDACTED}"}


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
        outcome=outcome or EXAMPLES["answered"],
    )


def test_transcripts_round_trip_for_every_outcome() -> None:
    turns = [_turn(0, calls=1, input_tokens=10), _turn(1, input_tokens=5, output_tokens=3)]
    for outcome in EXAMPLES.values():
        transcript = _transcript(turns, outcome)
        assert RunTranscript.model_validate_json(transcript.model_dump_json()) == transcript
    assert _transcript(turns).usage == TokenUsage(input_tokens=15, output_tokens=3)


def test_total_usage_keeps_unreported_counters_empty() -> None:
    assert total_usage([]) == TokenUsage()
    assert total_usage([_turn(0, output_tokens=2), _turn(1)]) == TokenUsage(output_tokens=2)


@pytest.mark.parametrize(
    "build",
    [
        lambda: _transcript([_turn(1)]),
        lambda: _transcript([_turn(0), _turn(0)]),
        lambda: _transcript(
            [_turn(index) for index in range(3)], limits=RunLimits(max_provider_requests=2)
        ),
        lambda: _transcript([_turn(0, calls=3)], limits=RunLimits(max_tool_calls=2)),
        lambda: _transcript([_turn(0, input_tokens=4)], usage=TokenUsage(input_tokens=5)),
        lambda: _transcript([], model=SECRET),
        lambda: _transcript([], provider_kind="Not A Kind"),
    ],
    ids=["unnumbered", "duplicate", "requests", "tool-calls", "usage", "secret-model", "kind"],
)
def test_inconsistent_transcripts_are_rejected(build) -> None:
    with pytest.raises(ValidationError):
        build()


def test_transcripts_have_no_credential_fields() -> None:
    def names(schema: dict) -> set[str]:
        found = set(schema.get("properties", {}))
        for value in schema.values():
            if isinstance(value, dict):
                found |= names(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        found |= names(item)
        return found

    fields = names(RunTranscript.model_json_schema())
    assert fields and not any(is_sensitive_path(name) for name in fields)


def test_no_credential_value_appears_in_a_transcript() -> None:
    call = ToolCall(
        id="call_1",
        name="search_resources",
        arguments=ParsedToolArguments(value={"name": SECRET, "note": f"Bearer {SECRET}"}),
    )
    turn = TranscriptTurn(
        request=0,
        latency_seconds=0.1,
        stop_reason=StopReason.TOOL_USE,
        text=transcript_text(f"Calling with {SECRET}"),
        tool_calls=(
            transcript_tool_call(call, ToolResult(call_id="call_1", content=f"echo {SECRET}")),
        ),
    )
    transcript = _transcript([turn], ProviderFailed.from_error(ProviderAuthenticationError()))
    assert SECRET not in transcript.model_dump_json()
    assert SECRET[:16] not in repr(transcript)


def test_transcripts_record_answers_redacted_and_bounded() -> None:
    from archdiscov.assistant.runs import transcript_outcome

    raw = Answered(
        message=AssistantMessage(text=f"Key {SECRET} " + "z" * 5_000), stop_reason=StopReason.END
    )
    recorded = transcript_outcome(raw)
    assert SECRET not in recorded.message.text
    assert len(recorded.message.text) <= MAX_TRANSCRIPT_TEXT_CHARS
    assert recorded.stop_reason == StopReason.END
    assert _transcript([], recorded).outcome == recorded
    for other in (EXAMPLES[kind] for kind in EXAMPLES if kind != "answered"):
        assert transcript_outcome(other) is other
    with pytest.raises(ValidationError) as raised:
        _transcript([], raw)
    assert raised.value.errors()[0]["type"] == "run_transcript_secret"
