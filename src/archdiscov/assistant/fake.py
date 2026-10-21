"""A scripted fake provider: the reference behaviour for orchestration tests.

A transcript is a strict JSON file of turns. Each provider attempt consumes the next
turn, which either returns a `CompletionResponse` or raises a typed provider error,
after an optional simulated delay. Nothing is improvised: running out of turns, an
unmet expectation, or a scripted response the request's limits forbid raises
`FakeScriptError`, which is a test failure, never a provider error.

Like a real adapter, the fake checks capabilities before consuming anything, retries
only retryable errors up to `max_retries` (each retry consumes a turn), and turns a
delay longer than the request timeout into `ProviderTimeoutError`. Time comes from an
injected clock, so delays never wait.
"""

import asyncio
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Protocol, Self

from pydantic import Field, ValidationError, model_validator

from archdiscov.models.validation_errors import (
    ValidationIssue,
    safe_validation_issues,
    validation_error,
)

from .conversation import Contract, MessageRole, ToolName
from .provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderCapability,
    ProviderError,
    ProviderErrorCode,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderTruncatedOutputError,
    ProviderUnavailableError,
    check_capabilities,
)

MAX_TRANSCRIPT_BYTES = 1_048_576

_SCRIPTABLE_ERRORS: Mapping[ProviderErrorCode, type[ProviderError]] = {
    error.code: error
    for error in (
        ProviderAuthenticationError,
        ProviderRateLimitError,
        ProviderTimeoutError,
        ProviderTransportError,
        ProviderInvalidRequestError,
        ProviderMalformedOutputError,
        ProviderTruncatedOutputError,
        ProviderRefusalError,
        ProviderUnavailableError,
    )
}


class FakeExpectation(Contract):
    """Optional checks on the request that consumes a turn; omitted fields are not checked."""

    tool_names: tuple[ToolName, ...] | None = None
    last_message_role: MessageRole | None = None


class _Turn(Contract):
    delay_seconds: float = Field(default=0.0, ge=0, le=600)
    expect: FakeExpectation | None = None
    ignore_request_limits: bool = False


class FakeRespond(_Turn):
    kind: Literal["respond"] = "respond"
    response: CompletionResponse


class FakeFail(_Turn):
    """Raise the typed provider error for `error`; capability errors are not scriptable."""

    kind: Literal["fail"] = "fail"
    error: ProviderErrorCode

    @model_validator(mode="after")
    def require_scriptable_error(self) -> Self:
        if self.error not in _SCRIPTABLE_ERRORS:
            raise validation_error("fake_transcript_error_unscriptable", "capability error")
        return self


FakeTurn = Annotated[FakeRespond | FakeFail, Field(discriminator="kind")]


class FakeTranscript(Contract):
    """The provider's capabilities and the turns it replays, in order."""

    capabilities: ProviderCapabilities = ProviderCapabilities(
        capabilities=frozenset(
            {ProviderCapability.TOOL_CALLING, ProviderCapability.PARALLEL_TOOL_CALLS}
        )
    )
    turns: tuple[FakeTurn, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_capabilities_for_scripted_calls(self) -> Self:
        supported = self.capabilities.capabilities
        for turn in self.turns:
            if not isinstance(turn, FakeRespond):
                continue
            calls = len(turn.response.message.tool_calls)
            if (calls and ProviderCapability.TOOL_CALLING not in supported) or (
                calls > 1 and ProviderCapability.PARALLEL_TOOL_CALLS not in supported
            ):
                raise validation_error(
                    "fake_transcript_capability_mismatch", "turn needs an unsupported capability"
                )
        return self


_DIAGNOSTIC_FIELDS = frozenset(
    field
    for model in (FakeTranscript, FakeRespond, FakeFail, FakeExpectation, CompletionResponse)
    for field in model.model_fields
)


class FakeScriptErrorCode(StrEnum):
    UNREADABLE = "fake_transcript_unreadable"
    TOO_LARGE = "fake_transcript_too_large"
    INVALID = "fake_transcript_invalid"
    EXHAUSTED = "fake_transcript_exhausted"
    EXPECTATION_MISMATCH = "fake_expectation_mismatch"
    LIMIT_EXCEEDED = "fake_turn_exceeds_limits"


class FakeScriptError(Exception):
    """The script and the test disagree; deliberately not a `ProviderError`."""

    messages: ClassVar[Mapping[FakeScriptErrorCode, str]] = {
        FakeScriptErrorCode.UNREADABLE: "Fake transcript file could not be read.",
        FakeScriptErrorCode.TOO_LARGE: f"Fake transcript exceeds {MAX_TRANSCRIPT_BYTES} bytes.",
        FakeScriptErrorCode.INVALID: "Fake transcript validation failed.",
        FakeScriptErrorCode.EXHAUSTED: "Fake transcript has no turn left for this request.",
        FakeScriptErrorCode.EXPECTATION_MISMATCH: (
            "Request does not match the scripted turn's expectation."
        ),
        FakeScriptErrorCode.LIMIT_EXCEEDED: "Scripted turn exceeds the request's limits.",
    }

    def __init__(
        self,
        code: FakeScriptErrorCode,
        *,
        turn: int | None = None,
        path: Path | None = None,
        details: tuple[ValidationIssue, ...] = (),
    ) -> None:
        self.code = FakeScriptErrorCode(code)
        location = "" if turn is None else f" (turn {turn})"
        super().__init__(self.messages[self.code] + location)
        self.turn = turn
        self.path = path
        self.details = details


def load_fake_transcript(path: Path) -> FakeTranscript:
    """Read a bounded JSON transcript file and validate it."""
    try:
        with path.open("rb") as file:
            content = file.read(MAX_TRANSCRIPT_BYTES + 1)
    except OSError as exc:
        raise FakeScriptError(FakeScriptErrorCode.UNREADABLE, path=path) from exc
    if len(content) > MAX_TRANSCRIPT_BYTES:
        raise FakeScriptError(FakeScriptErrorCode.TOO_LARGE, path=path)
    try:
        return FakeTranscript.model_validate_json(content)
    except ValidationError as exc:
        raise FakeScriptError(
            FakeScriptErrorCode.INVALID,
            path=path,
            details=safe_validation_issues(exc, _DIAGNOSTIC_FIELDS, "fake transcript"),
        ) from exc


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    async def sleep_async(self, seconds: float) -> None: ...


class FakeClock:
    """Advances only when slept on; async sleeps still yield to the event loop."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds

    async def sleep_async(self, seconds: float) -> None:
        self._now += seconds
        await asyncio.sleep(0)


class FakeRequestRecord(Contract):
    """One attempt as the fake received it, with the clock reading at receipt."""

    request: CompletionRequest
    received_at: float


class FakeProvider:
    """Replays a transcript strictly; satisfies the `Provider` protocol."""

    def __init__(
        self,
        transcript: FakeTranscript,
        *,
        clock: Clock | None = None,
        max_retries: int = 0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self._transcript = transcript
        self._clock = FakeClock() if clock is None else clock
        self._max_retries = max_retries
        self._next = 0
        self._records: list[FakeRequestRecord] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._transcript.capabilities

    @property
    def requests(self) -> tuple[FakeRequestRecord, ...]:
        return tuple(self._records)

    @property
    def remaining_turns(self) -> int:
        return len(self._transcript.turns) - self._next

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        check_capabilities(self.capabilities, request)
        for attempt in range(self._max_retries + 1):
            index, turn = self._take(request)
            delay = min(turn.delay_seconds, request.limits.timeout_seconds)
            self._clock.sleep(delay)
            try:
                return self._outcome(index, turn, request)
            except ProviderError as error:
                if not error.retryable or attempt == self._max_retries:
                    raise
        raise AssertionError("unreachable")

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        check_capabilities(self.capabilities, request)
        for attempt in range(self._max_retries + 1):
            index, turn = self._take(request)
            delay = min(turn.delay_seconds, request.limits.timeout_seconds)
            await self._clock.sleep_async(delay)
            try:
                return self._outcome(index, turn, request)
            except ProviderError as error:
                if not error.retryable or attempt == self._max_retries:
                    raise
        raise AssertionError("unreachable")

    def _take(self, request: CompletionRequest) -> tuple[int, FakeRespond | FakeFail]:
        index = self._next
        if index >= len(self._transcript.turns):
            raise FakeScriptError(FakeScriptErrorCode.EXHAUSTED, turn=index)
        turn = self._transcript.turns[index]
        self._records.append(
            FakeRequestRecord(request=request, received_at=self._clock.monotonic())
        )
        self._next += 1
        expect = turn.expect
        if expect is not None and (
            (
                expect.tool_names is not None
                and tuple(tool.name for tool in request.tools) != expect.tool_names
            )
            or (
                expect.last_message_role is not None
                and request.conversation.messages[-1].role != expect.last_message_role
            )
        ):
            raise FakeScriptError(FakeScriptErrorCode.EXPECTATION_MISMATCH, turn=index)
        return index, turn

    @staticmethod
    def _outcome(
        index: int, turn: FakeRespond | FakeFail, request: CompletionRequest
    ) -> CompletionResponse:
        if turn.delay_seconds > request.limits.timeout_seconds:
            raise ProviderTimeoutError()
        if isinstance(turn, FakeFail):
            raise _SCRIPTABLE_ERRORS[turn.error]()
        response = turn.response
        output_tokens = response.usage.output_tokens
        if not turn.ignore_request_limits and (
            len(response.message.tool_calls) > request.limits.max_tool_calls_per_turn
            or (output_tokens is not None and output_tokens > request.limits.max_output_tokens)
        ):
            raise FakeScriptError(FakeScriptErrorCode.LIMIT_EXCEEDED, turn=index)
        return response
