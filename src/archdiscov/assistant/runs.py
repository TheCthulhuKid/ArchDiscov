"""Run limits, terminal outcomes, and transcripts for one assistant run.

These contracts are independent of the loop that produces them. A run ends with exactly
one `RunOutcome`; a limit ends it with `limit_reached`, never an unbounded retry. A
`RunTranscript` records what happened for evaluation and debugging: bounded, sanitized
text only, with secret-looking spans redacted, and no credential fields at all.
"""

import json
from collections.abc import Iterable, Iterator
from enum import StrEnum
from itertools import count
from typing import Annotated, Any, Literal, Self

from pydantic import Field, JsonValue, NonNegativeFloat, NonNegativeInt, model_validator

from archdiscov.models.sanitization import sanitize_value
from archdiscov.models.validation_errors import validation_error

from .answers import MAX_EXPLANATION_CHARS, MAX_SUMMARY_CHARS, AnswerProposal
from .conversation import (
    AssistantMessage,
    Contract,
    MalformedArgumentsReason,
    MalformedToolArguments,
    TokenUsage,
    ToolCall,
    ToolCallId,
    ToolName,
    ToolResult,
)
from .provider import ProviderError, ProviderErrorCode, RequestLimits, StopReason
from .redaction import REDACTED, looks_secret, redact_secrets

MAX_TRANSCRIPT_TEXT_CHARS = 4_000
# Redaction scans a bounded window: anything past it is cut by truncation anyway.
_REDACTION_WINDOW_CHARS = 4 * MAX_TRANSCRIPT_TEXT_CHARS


class RunLimits(Contract):
    """Bounds for one run; defaults are conservative and every bound is explicit."""

    max_provider_requests: int = Field(default=8, ge=1, le=50, strict=True)
    max_tool_calls_per_turn: int = Field(default=1, ge=1, le=32, strict=True)
    max_tool_calls: int = Field(default=32, ge=1, le=200, strict=True)
    max_output_tokens: int = Field(default=4_096, ge=1, le=128_000, strict=True)
    max_total_output_chars: int = Field(default=200_000, ge=1_000, le=1_000_000, strict=True)
    """Characters of assistant text and tool-result content added to the conversation."""
    request_timeout_seconds: float = Field(default=60.0, gt=0, le=600, strict=True)
    overall_timeout_seconds: float = Field(default=300.0, gt=0, le=3_600, strict=True)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if (
            self.max_tool_calls_per_turn > self.max_tool_calls
            or self.request_timeout_seconds > self.overall_timeout_seconds
        ):
            raise validation_error("run_limits_order", "per-request limits exceed run limits")
        return self

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "RunLimits":
        """Limits from a step 34.3 provider configuration; `overrides` replace any field."""
        values = {
            "max_provider_requests": config.max_requests,
            "max_tool_calls_per_turn": config.max_tool_calls_per_turn,
            "max_tool_calls": config.max_tool_calls,
            "max_output_tokens": config.max_output_tokens,
            "request_timeout_seconds": config.request_timeout_seconds,
            "overall_timeout_seconds": config.max_latency_seconds,
        }
        return cls(**{**values, **overrides})

    def request_limits(self, *, timeout_seconds: float | None = None) -> RequestLimits:
        """Per-request provider limits; a shorter remaining deadline may tighten the timeout."""
        timeout = self.request_timeout_seconds
        if timeout_seconds is not None:
            timeout = min(timeout, timeout_seconds)
        return RequestLimits(
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=timeout,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
        )


# ---- outcomes ----------------------------------------------------------------------


class RunLimitKind(StrEnum):
    PROVIDER_REQUESTS = "provider_requests"
    TOOL_CALLS_PER_TURN = "tool_calls_per_turn"
    TOOL_CALLS = "tool_calls"
    TOTAL_OUTPUT = "total_output"
    REQUEST_DEADLINE = "request_deadline"
    OVERALL_DEADLINE = "overall_deadline"


class MalformedOutputReason(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    """The provider raised `provider_malformed_output`."""
    CONVERSATION_INVALID = "conversation_invalid"
    """The response cannot extend the conversation (for example, a reused call ID)."""
    TOOL_CALLS_WITHOUT_TOOLS = "tool_calls_without_tools"
    """The response called tools although none were offered."""
    PROPOSAL_MALFORMED = "proposal_malformed"
    """`submit_answer` arguments were not a JSON object within the size limit."""
    PROPOSAL_INVALID = "proposal_invalid"
    """`submit_answer` arguments did not match the answer proposal contract."""
    PROPOSAL_MIXED = "proposal_mixed"
    """`submit_answer` was called together with other tools."""


class Answered(Contract):
    """The provider's final assistant message, returned as-is for step 37 to judge."""

    kind: Literal["answered"] = "answered"
    message: AssistantMessage
    stop_reason: Literal[StopReason.END, StopReason.LENGTH, StopReason.REFUSAL]

    @model_validator(mode="after")
    def require_final_message(self) -> Self:
        if self.message.tool_calls or self.message.text is None:
            raise validation_error("run_answer_not_final", "answers are text without tool calls")
        return self


class Proposed(Contract):
    """The structured answer submitted through `submit_answer`, for validation (step 37)."""

    kind: Literal["proposed"] = "proposed"
    proposal: AnswerProposal


class LimitReached(Contract):
    kind: Literal["limit_reached"] = "limit_reached"
    limit: RunLimitKind


class ProviderFailed(Contract):
    """A typed provider error, with its fixed message; never provider-supplied text."""

    kind: Literal["provider_error"] = "provider_error"
    code: ProviderErrorCode
    retryable: bool
    message: str

    @model_validator(mode="after")
    def exclude_other_outcomes(self) -> Self:
        if self.code == ProviderErrorCode.MALFORMED_OUTPUT:
            raise validation_error("run_outcome_kind_mismatch", "malformed output has its own kind")
        return self

    @classmethod
    def from_error(cls, error: ProviderError) -> "ProviderFailed":
        return cls(code=error.code, retryable=error.retryable, message=error.message)


class MalformedOutput(Contract):
    kind: Literal["malformed_output"] = "malformed_output"
    reason: MalformedOutputReason


class Cancelled(Contract):
    kind: Literal["cancelled"] = "cancelled"


RunOutcome = Annotated[
    Answered | Proposed | LimitReached | ProviderFailed | MalformedOutput | Cancelled,
    Field(discriminator="kind"),
]


def _redacted_id(value: str, placeholders: Iterator[int]) -> str:
    return f"{REDACTED}-{next(placeholders)}" if looks_secret(value) else value


def _recorded_proposal(proposal: AnswerProposal) -> AnswerProposal:
    """Narrative redacted and bounded; a secret-looking ID becomes a unique placeholder."""
    data = proposal.model_dump(mode="json")
    placeholders = count(1)
    data["summary"] = transcript_text(data["summary"]).text[:MAX_SUMMARY_CHARS]
    if data["explanation"] is not None:
        data["explanation"] = transcript_text(data["explanation"]).text[:MAX_EXPLANATION_CHARS]
    for conclusion in data["conclusions"]:
        for citation in conclusion["citations"]:
            for key in ("finding_ids", "evidence_ids", "resource_ids"):
                citation[key] = [_redacted_id(value, placeholders) for value in citation[key]]
    for limitation in data["limitations"]:
        limitation["finding_id"] = _redacted_id(limitation["finding_id"], placeholders)
    return AnswerProposal.model_validate(data)


def transcript_outcome(outcome: RunOutcome) -> RunOutcome:
    """The outcome as a transcript records it: answer text and proposal narrative are
    redacted and bounded, and secret-looking cited IDs are replaced."""
    if isinstance(outcome, Answered):
        text = transcript_text(outcome.message.text)
        return Answered(message=AssistantMessage(text=text.text), stop_reason=outcome.stop_reason)
    if isinstance(outcome, Proposed):
        return Proposed(proposal=_recorded_proposal(outcome.proposal))
    return outcome


def provider_outcome(error: ProviderError) -> ProviderFailed | MalformedOutput:
    """The outcome for a provider error; malformed output gets its own kind."""
    if error.code == ProviderErrorCode.MALFORMED_OUTPUT:
        return MalformedOutput(reason=MalformedOutputReason.PROVIDER_REPORTED)
    return ProviderFailed.from_error(error)


# ---- transcripts -------------------------------------------------------------------


class TranscriptText(Contract):
    """Bounded text with secret-looking spans redacted; `chars` is the original length.

    Redaction changes lengths, so `truncated` (not `chars`) says whether text was cut.
    """

    text: str = Field(max_length=MAX_TRANSCRIPT_TEXT_CHARS)
    truncated: bool
    chars: NonNegativeInt

    @model_validator(mode="after")
    def reject_secrets(self) -> Self:
        if looks_secret(self.text):
            raise validation_error("run_transcript_secret", "secret-looking transcript text")
        return self


def transcript_text(text: str) -> TranscriptText:
    window = redact_secrets(text[:_REDACTION_WINDOW_CHARS])
    kept = window[:MAX_TRANSCRIPT_TEXT_CHARS]
    truncated = len(window) > len(kept) or len(text) > _REDACTION_WINDOW_CHARS
    return TranscriptText(text=kept, truncated=truncated, chars=len(text))


def transcript_json(value: JsonValue) -> TranscriptText:
    """Compact JSON of a value after redacting sensitive keys and secret-looking text."""
    sanitized, _ = sanitize_value(value)
    return transcript_text(
        json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


class TranscriptToolCall(Contract):
    call_id: ToolCallId
    name: ToolName
    arguments: TranscriptText | None
    """Sanitized compact JSON; None when the arguments were malformed."""
    malformed_arguments: MalformedArgumentsReason | None = None
    result: TranscriptText
    result_is_error: bool

    @model_validator(mode="after")
    def require_one_argument_form(self) -> Self:
        if (self.arguments is None) == (self.malformed_arguments is None):
            raise validation_error("run_transcript_arguments", "parsed or malformed, not both")
        return self


def transcript_tool_call(call: ToolCall, result: ToolResult) -> TranscriptToolCall:
    if call.id != result.call_id:
        raise ValueError("tool result must answer the tool call")
    malformed = isinstance(call.arguments, MalformedToolArguments)
    return TranscriptToolCall(
        call_id=call.id,
        name=call.name,
        arguments=None if malformed else transcript_json(call.arguments.value),
        malformed_arguments=call.arguments.reason if malformed else None,
        result=transcript_text(result.content),
        result_is_error=result.is_error,
    )


class TranscriptTurn(Contract):
    """One provider request and what followed it."""

    request: NonNegativeInt
    latency_seconds: NonNegativeFloat
    stop_reason: StopReason | None
    """None when the request ended in an error or the run ended first."""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    text: TranscriptText | None = None
    tool_calls: tuple[TranscriptToolCall, ...] = Field(default=(), max_length=32)
    error: ProviderErrorCode | None = None


def total_usage(turns: Iterable[TranscriptTurn]) -> TokenUsage:
    """Sum each counter that any turn reported; counters no turn reported stay None."""
    totals: dict[str, int | None] = dict.fromkeys(TokenUsage.model_fields)
    for turn in turns:
        for name in totals:
            value = getattr(turn.usage, name)
            if value is not None:
                totals[name] = (totals[name] or 0) + value
    return TokenUsage(**totals)


class RunTranscript(Contract):
    """What a run did, for evaluation and debugging; holds no credential fields."""

    provider_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    model: Annotated[str, Field(min_length=1, max_length=256)]
    limits: RunLimits
    turns: tuple[TranscriptTurn, ...] = Field(default=(), max_length=50)
    usage: TokenUsage
    latency_seconds: NonNegativeFloat
    outcome: RunOutcome

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if looks_secret(self.model):
            raise validation_error("run_transcript_secret", "secret-looking model name")
        if isinstance(self.outcome, Answered):
            text = self.outcome.message.text
            if len(text) > MAX_TRANSCRIPT_TEXT_CHARS or looks_secret(text):
                raise validation_error(
                    "run_transcript_secret", "record answers with transcript_outcome"
                )
        if isinstance(self.outcome, Proposed) and looks_secret(
            self.outcome.proposal.model_dump_json()
        ):
            raise validation_error(
                "run_transcript_secret", "record proposals with transcript_outcome"
            )
        if [turn.request for turn in self.turns] != list(range(len(self.turns))):
            raise validation_error("run_transcript_counts", "turns are numbered from zero")
        if len(self.turns) > self.limits.max_provider_requests:
            raise validation_error("run_transcript_counts", "more turns than requests allowed")
        if sum(len(turn.tool_calls) for turn in self.turns) > self.limits.max_tool_calls:
            raise validation_error("run_transcript_counts", "more tool calls than allowed")
        if self.usage != total_usage(self.turns):
            raise validation_error("run_transcript_counts", "usage must total the turns")
        return self
