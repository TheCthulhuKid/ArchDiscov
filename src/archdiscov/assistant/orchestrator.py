"""The tool-calling loop: provider turn, tool execution, repeat until an answer or a limit.

The loop is a state machine with typed terminal outcomes (`runs.RunOutcome`). It makes no
judgement about answer content: the final assistant message is returned as-is for answer
validation (step 37), and nothing here retries a rejected answer. Provider errors end the
run; providers retry only their own transport and rate-limit errors.

`_Run` holds the state and the transitions; drivers decide only how a provider is called.
"""

import time
from collections.abc import Callable

from pydantic import ValidationError

from .conversation import (
    AssistantMessage,
    Contract,
    Conversation,
    TokenUsage,
    ToolResult,
    ToolResultMessage,
)
from .instructions import initial_conversation
from .provider import CompletionRequest, CompletionResponse, Provider, ProviderError, complete
from .runs import (
    Answered,
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    RunLimitKind,
    RunLimits,
    RunOutcome,
    RunTranscript,
    TranscriptTurn,
    provider_outcome,
    total_usage,
    transcript_outcome,
    transcript_text,
    transcript_tool_call,
)
from .tools.catalog import default_registry
from .tools.record import ReturnedIds
from .tools.registry import ToolRegistry
from .tools.session import ToolSession


class RunResult(Contract):
    """How the run ended, what it did, the final conversation, and the IDs tools returned."""

    outcome: RunOutcome
    transcript: RunTranscript
    conversation: Conversation
    returned: ReturnedIds


class _Run:
    """Run state and transitions, independent of how the provider is called."""

    def __init__(
        self,
        question: str,
        *,
        session: ToolSession,
        registry: ToolRegistry,
        limits: RunLimits,
        provider_kind: str,
        model: str,
        clock: Callable[[], float],
    ) -> None:
        self.session = session
        self.registry = registry
        self.limits = limits
        self.provider_kind = provider_kind
        self.model = model
        self.clock = clock
        self.tools = registry.declarations()
        self.messages = list(initial_conversation(question).messages)
        self.turns: list[TranscriptTurn] = []
        self.tool_calls = 0
        self.output_chars = 0
        self.started = clock()
        self.request_started = self.started
        self.outcome: RunOutcome | None = None

    def next_request(self) -> CompletionRequest | None:
        """The next provider request, or None once the run has an outcome."""
        if self.outcome is not None:
            return None
        if len(self.turns) >= self.limits.max_provider_requests:
            self.outcome = LimitReached(limit=RunLimitKind.PROVIDER_REQUESTS)
            return None
        self.request_started = self.clock()
        return CompletionRequest(
            conversation=Conversation(messages=tuple(self.messages)),
            tools=self.tools,
            limits=self.limits.request_limits(),
        )

    def fail(self, error: ProviderError) -> None:
        self._turn(error=error)
        self.outcome = provider_outcome(error)

    def respond(self, response: CompletionResponse) -> None:
        """Record the response, then answer, end on a limit, or run its tool calls."""
        message = response.message
        calls = message.tool_calls
        self.output_chars += len(message.text or "")
        if calls and not self.tools:
            self._end(
                response, MalformedOutput(reason=MalformedOutputReason.TOOL_CALLS_WITHOUT_TOOLS)
            )
        elif len(calls) > self.limits.max_tool_calls_per_turn:
            self._end(response, LimitReached(limit=RunLimitKind.TOOL_CALLS_PER_TURN))
        elif self.tool_calls + len(calls) > self.limits.max_tool_calls:
            self._end(response, LimitReached(limit=RunLimitKind.TOOL_CALLS))
        elif self.output_chars > self.limits.max_total_output_chars:
            self._end(response, LimitReached(limit=RunLimitKind.TOTAL_OUTPUT))
        elif not self._extends_conversation(message):
            self._end(response, MalformedOutput(reason=MalformedOutputReason.CONVERSATION_INVALID))
        elif not calls:
            self.messages.append(message)
            self._end(response, Answered(message=message, stop_reason=response.stop_reason))
        elif len(self.turns) + 1 >= self.limits.max_provider_requests:
            # No request is left to read the results, so the calls are not run.
            self.messages.append(message)
            self._end(response, LimitReached(limit=RunLimitKind.PROVIDER_REQUESTS))
        else:
            self._run_tools(response)

    def _extends_conversation(self, message: AssistantMessage) -> bool:
        try:
            Conversation(messages=(*self.messages, message))
        except ValidationError:
            return False
        return True

    def _run_tools(self, response: CompletionResponse) -> None:
        message = response.message
        results = tuple(self.registry.dispatch(self.session, call) for call in message.tool_calls)
        self.tool_calls += len(results)
        self.output_chars += sum(len(result.content) for result in results)
        self.messages.extend((message, ToolResultMessage(results=results)))
        self._turn(response=response, results=results)
        if self.output_chars > self.limits.max_total_output_chars:
            self.outcome = LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)

    def _end(self, response: CompletionResponse, outcome: RunOutcome) -> None:
        self._turn(response=response)
        self.outcome = outcome

    def _turn(
        self,
        *,
        response: CompletionResponse | None = None,
        results: tuple[ToolResult, ...] = (),
        error: ProviderError | None = None,
    ) -> None:
        """Record one request; only calls that were run (and so have results) are listed."""
        message = None if response is None else response.message
        text = None if message is None else message.text
        calls = () if message is None else message.tool_calls
        self.turns.append(
            TranscriptTurn(
                request=len(self.turns),
                latency_seconds=max(0.0, self.clock() - self.request_started),
                stop_reason=None if response is None else response.stop_reason,
                usage=TokenUsage() if response is None else response.usage,
                text=None if text is None else transcript_text(text),
                tool_calls=tuple(
                    transcript_tool_call(call, result)
                    for call, result in zip(calls[: len(results)], results, strict=True)
                ),
                error=None if error is None else error.code,
            )
        )

    def result(self) -> RunResult:
        assert self.outcome is not None
        transcript = RunTranscript(
            provider_kind=self.provider_kind,
            model=self.model,
            limits=self.limits,
            turns=tuple(self.turns),
            usage=total_usage(self.turns),
            latency_seconds=max(0.0, self.clock() - self.started),
            outcome=transcript_outcome(self.outcome),
        )
        return RunResult(
            outcome=self.outcome,
            transcript=transcript,
            conversation=Conversation(messages=tuple(self.messages)),
            returned=self.session.record.returned(),
        )


def run_assistant(
    question: str,
    *,
    provider: Provider,
    session: ToolSession,
    provider_kind: str,
    model: str,
    limits: RunLimits | None = None,
    registry: ToolRegistry | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Answer one question over the session's selected data, or end with a typed outcome.

    Use one session per run: the returned IDs are the session's run record.
    """
    run = _Run(
        question,
        session=session,
        registry=default_registry() if registry is None else registry,
        limits=RunLimits() if limits is None else limits,
        provider_kind=provider_kind,
        model=model,
        clock=clock,
    )
    while (request := run.next_request()) is not None:
        try:
            response = complete(provider, request)
        except ProviderError as error:
            run.fail(error)
        else:
            run.respond(response)
    return run.result()
