"""The tool-calling loop: provider turn, tool execution, repeat until an answer or a limit.

The loop is a state machine with typed terminal outcomes (`runs.RunOutcome`). It makes no
judgement about answer content: a `submit_answer` proposal or a final text answer is
returned as-is for answer validation. Provider errors end the run; providers retry only
their own transport and rate-limit errors.

A `repair` callback, when given, sees the outcome of a run that ended on a response and may
return a repair prompt. The loop then adds it, offers only `submit_answer`, and makes one
reserved request outside `max_provider_requests`, unless the overall deadline or the
total-output limit leaves no room. There is never a second repair: deciding whether the
repaired answer is acceptable is left to the caller.

Deadlines use the injected clock. Each request's timeout is the shorter of the request
deadline and the time left overall, and is passed to the provider, which enforces it: its
timeout is a `provider_timeout` error, or `overall_deadline` when the time left was shorter.
The loop also checks the clock after every response and tool batch, ending with
`request_deadline` or `overall_deadline`, and a real-time guard cancels a provider call that
ignores its timeout. An abandoned call is cancelled and allowed to settle before the run
ends; its result is discarded.

`_Run` holds the state and the transitions; `run_assistant_async` is the one driver, and
`run_assistant` runs it to completion from synchronous code.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from .answers import SUBMIT_ANSWER, submit_answer_declaration
from .conversation import (
    AssistantMessage,
    Contract,
    Conversation,
    Message,
    TokenUsage,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from .instructions import initial_conversation
from .provider import (
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    ProviderErrorCode,
    complete_async,
)
from .runs import (
    Answered,
    Cancelled,
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    RunLimitKind,
    RunLimits,
    RunOutcome,
    RunTranscript,
    TranscriptRepair,
    TranscriptTurn,
    provider_outcome,
    read_proposal,
    total_usage,
    transcript_outcome,
    transcript_text,
    transcript_tool_call,
)
from .tools.catalog import default_registry
from .tools.record import ReturnedIds
from .tools.registry import ToolRegistry
from .tools.session import ToolSession

RepairPrompt = Callable[[RunOutcome, AssistantMessage], tuple[Message, ...] | None]


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
        offer_answer_tool: bool,
        repairable: bool = False,
    ) -> None:
        self.session = session
        self.registry = registry
        self.limits = limits
        self.provider_kind = provider_kind
        self.model = model
        self.clock = clock
        if offer_answer_tool and SUBMIT_ANSWER in registry.tools:
            raise ValueError(f"a registered tool may not be named {SUBMIT_ANSWER}")
        if repairable and not offer_answer_tool:
            raise ValueError(f"a repair needs {SUBMIT_ANSWER} to be offered")
        self.offer_answer_tool = offer_answer_tool
        self.tools = registry.declarations() + (
            (submit_answer_declaration(),) if offer_answer_tool else ()
        )
        self.messages = list(initial_conversation(question).messages)
        self.turns: list[TranscriptTurn] = []
        self.tool_calls = 0
        self.output_chars = 0
        self.started = clock()
        self.request_started = self.started
        self.request_timeout = limits.request_timeout_seconds
        self.in_flight = False
        self.outcome: RunOutcome | None = None
        self.final: AssistantMessage | None = None
        self.repair: TranscriptRepair | None = None

    def next_request(self) -> CompletionRequest | None:
        """The next provider request, or None once the run has an outcome."""
        if self.outcome is not None:
            return None
        if self.repair is None and len(self.turns) >= self.limits.max_provider_requests:
            self.outcome = LimitReached(limit=RunLimitKind.PROVIDER_REQUESTS)
            return None
        remaining = self.limits.overall_timeout_seconds - (self.clock() - self.started)
        if remaining <= 0:
            self.outcome = LimitReached(limit=RunLimitKind.OVERALL_DEADLINE)
            return None
        self.request_started = self.clock()
        limits = self.limits.request_limits(timeout_seconds=remaining)
        self.request_timeout = limits.timeout_seconds
        self.in_flight = True
        return CompletionRequest(
            conversation=Conversation(messages=tuple(self.messages)),
            tools=self.tools,
            limits=limits,
        )

    def fail(self, error: ProviderError) -> None:
        self.in_flight = False
        self._turn(error=error)
        if (
            error.code == ProviderErrorCode.TIMEOUT
            and self.request_timeout < self.limits.request_timeout_seconds
        ):
            # The timeout was the time left overall, so the overall deadline ended the run.
            self.outcome = LimitReached(limit=RunLimitKind.OVERALL_DEADLINE)
        else:
            self.outcome = provider_outcome(error)

    def timed_out(self) -> None:
        """The provider call outlived its timeout and was abandoned."""
        self.in_flight = False
        self._turn()
        self.outcome = LimitReached(limit=RunLimitKind.REQUEST_DEADLINE)

    def cancel(self) -> None:
        """End the run where it is; an in-flight request is recorded without a response."""
        if self.in_flight:
            self.in_flight = False
            self._turn()
        self.outcome = Cancelled()

    def _overrun(self) -> RunLimitKind | None:
        now = self.clock()
        if now - self.request_started > self.request_timeout:
            return RunLimitKind.REQUEST_DEADLINE
        if now - self.started > self.limits.overall_timeout_seconds:
            return RunLimitKind.OVERALL_DEADLINE
        return None

    def respond(self, response: CompletionResponse) -> None:
        """Discard a late response, reject one that cannot continue the run, or accept it."""
        self.in_flight = False
        message = response.message
        continuation = 0 if message.continuation is None else message.continuation.size
        self.output_chars += len(message.text or "") + continuation
        if (overrun := self._overrun()) is not None:
            # A late response is discarded: the deadline already decided the run.
            self._turn()
            self.outcome = LimitReached(limit=overrun)
        elif (rejection := self._rejection(response.message)) is not None:
            self._end(response, rejection)
        else:
            self.messages.append(response.message)
            self._accept(response)

    def _rejection(self, message: AssistantMessage) -> RunOutcome | None:
        """Why the response cannot be added to the conversation, or None.

        The checks run in `_REJECTIONS` order and the first that fails decides.
        """
        return next((outcome for fails, outcome in _REJECTIONS if fails(self, message)), None)

    def _calls_without_tools(self, message: AssistantMessage) -> bool:
        return bool(message.tool_calls) and not self.tools

    def _foreign_continuation(self, message: AssistantMessage) -> bool:
        # Another provider's blocks would be replayed to this one.
        continuation = message.continuation
        return continuation is not None and continuation.provider_kind != self.provider_kind

    def _over_total_output(self, _: AssistantMessage) -> bool:
        return self.output_chars > self.limits.max_total_output_chars

    def _breaks_conversation(self, message: AssistantMessage) -> bool:
        try:
            Conversation(messages=(*self.messages, message))
        except ValidationError:
            return True
        return False

    def _mixes_proposal(self, message: AssistantMessage) -> bool:
        return self._submits(message) and len(message.tool_calls) > 1

    def _calls_unoffered_tools(self, message: AssistantMessage) -> bool:
        # During the repair, no request is left to read their results, and they were not offered.
        return self.repair is not None and bool(message.tool_calls) and not self._submits(message)

    def _over_turn_calls(self, message: AssistantMessage) -> bool:
        return len(message.tool_calls) > self.limits.max_tool_calls_per_turn

    def _over_run_calls(self, message: AssistantMessage) -> bool:
        calls = self.tool_calls + len(message.tool_calls)
        return not self._submits(message) and calls > self.limits.max_tool_calls

    def _accept(self, response: CompletionResponse) -> None:
        """End with a proposal or an answer, end when no request is left, or run the tools."""
        message = response.message
        if self._submits(message):
            self._end(response, self._proposal_outcome(message))
        elif not message.tool_calls:
            self._end(response, Answered(message=message, stop_reason=response.stop_reason))
        elif len(self.turns) + 1 >= self.limits.max_provider_requests:
            # No request is left to read the results, so the calls are not run.
            self._end(response, LimitReached(limit=RunLimitKind.PROVIDER_REQUESTS))
        else:
            self._run_tools(response)

    def _submits(self, message: AssistantMessage) -> bool:
        return self.offer_answer_tool and any(
            call.name == SUBMIT_ANSWER for call in message.tool_calls
        )

    def _proposal_outcome(self, message: AssistantMessage) -> RunOutcome:
        """Read the lone `submit_answer` call; it is never dispatched as a tool."""
        return read_proposal(message.tool_calls[0].arguments)

    def _run_tools(self, response: CompletionResponse) -> None:
        message = response.message
        results = tuple(self.registry.dispatch(self.session, call) for call in message.tool_calls)
        self.tool_calls += len(results)
        self.output_chars += sum(len(result.content) for result in results)
        self.messages.append(ToolResultMessage(results=results))
        self._turn(response=response, results=results)
        if self.output_chars > self.limits.max_total_output_chars:
            self.outcome = LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)
        elif self.clock() - self.started > self.limits.overall_timeout_seconds:
            self.outcome = LimitReached(limit=RunLimitKind.OVERALL_DEADLINE)

    def _end(self, response: CompletionResponse, outcome: RunOutcome) -> None:
        self._turn(response=response)
        self.outcome = outcome
        self.final = response.message

    def begin_repair(self, prompt: tuple[Message, ...]) -> None:
        """Add the repair prompt after the response that ended the run; offer only the answer.

        The prompt counts toward the total-output limit; with no room left, the repair is
        not requested.
        """
        if self.outcome is None or self.final is None or self.repair is not None:
            raise ValueError("only a run that ended on a response is repaired, and only once")
        if self.messages[-1] is not self.final:
            # A mixed submission ended the run before joining the conversation; the prompt
            # answers its calls, so it joins now.
            self.messages.append(self.final)
        self.messages.extend(prompt)
        texts = [_prompt_text(message) for message in prompt]
        self.output_chars += sum(len(text) for text in texts)
        self.repair = TranscriptRepair(
            outcome=transcript_outcome(self.outcome), prompt=transcript_text("\n".join(texts))
        )
        self.tools = (submit_answer_declaration(),)
        self.outcome = None
        if self.output_chars >= self.limits.max_total_output_chars:
            self.outcome = LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)

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
                repair=self.repair is not None,
                continuation_bytes=(
                    None
                    if message is None or message.continuation is None
                    else message.continuation.size
                ),
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
            repair=self.repair,
        )
        return RunResult(
            outcome=self.outcome,
            transcript=transcript,
            conversation=Conversation(messages=tuple(self.messages)),
            returned=self.session.record.returned(),
        )


# Each check a response must pass, in order, and the outcome that ends the run if it fails.
_REJECTIONS: tuple[tuple[Callable[[_Run, AssistantMessage], bool], RunOutcome], ...] = (
    (
        _Run._calls_without_tools,
        MalformedOutput(reason=MalformedOutputReason.TOOL_CALLS_WITHOUT_TOOLS),
    ),
    (
        _Run._foreign_continuation,
        MalformedOutput(reason=MalformedOutputReason.CONTINUATION_FOREIGN),
    ),
    (_Run._over_total_output, LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)),
    (
        _Run._breaks_conversation,
        MalformedOutput(reason=MalformedOutputReason.CONVERSATION_INVALID),
    ),
    (_Run._mixes_proposal, MalformedOutput(reason=MalformedOutputReason.PROPOSAL_MIXED)),
    (
        _Run._calls_unoffered_tools,
        MalformedOutput(reason=MalformedOutputReason.TOOL_NOT_OFFERED),
    ),
    (_Run._over_turn_calls, LimitReached(limit=RunLimitKind.TOOL_CALLS_PER_TURN)),
    (_Run._over_run_calls, LimitReached(limit=RunLimitKind.TOOL_CALLS)),
)


def _prompt_text(message: Message) -> str:
    if isinstance(message, ToolResultMessage):
        return "\n".join(result.content for result in message.results)
    if isinstance(message, UserMessage):
        return message.text
    raise ValueError("a repair prompt holds user and tool-result messages only")


class RunCancelled(asyncio.CancelledError):
    """Cancellation of a run, carrying its partial result with the `cancelled` outcome.

    It is a `CancelledError`, so cancellation still propagates as asyncio expects.
    """

    def __init__(self, result: RunResult) -> None:
        super().__init__("Assistant run was cancelled.")
        self.result = result


class RunInEventLoopError(RuntimeError):
    """A synchronous wrapper was called where an event loop is already running."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name} cannot be used inside a running event loop; await {name}_async instead."
        )


async def _settle(task: asyncio.Future) -> None:
    """Cancel a provider call and wait until it has actually finished; discard its result."""
    task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not task.cancelled():
        task.exception()


async def _call(provider: Provider, request: CompletionRequest) -> CompletionResponse | None:
    """The provider's response, or None when it outlived the request timeout."""
    task = asyncio.ensure_future(complete_async(provider, request))
    try:
        return await asyncio.wait_for(asyncio.shield(task), request.limits.timeout_seconds)
    except TimeoutError:
        await _settle(task)
        return None
    except asyncio.CancelledError:
        await _settle(task)
        raise


async def run_assistant_async(
    question: str,
    *,
    provider: Provider,
    session: ToolSession,
    provider_kind: str,
    model: str,
    limits: RunLimits | None = None,
    registry: ToolRegistry | None = None,
    clock: Callable[[], float] = time.monotonic,
    offer_answer_tool: bool = True,
    repair: RepairPrompt | None = None,
) -> RunResult:
    """Answer one question over the session's selected data, or end with a typed outcome.

    `submit_answer` is offered after the registry's tools unless `offer_answer_tool` is
    false. `repair` is consulted at most once (see the module docstring). Use one session
    per run: the returned IDs are the session's run record. Cancelling the task raises
    `RunCancelled`, whose `result` holds the partial transcript.
    """
    run = _Run(
        question,
        session=session,
        registry=default_registry() if registry is None else registry,
        limits=RunLimits() if limits is None else limits,
        provider_kind=provider_kind,
        model=model,
        clock=clock,
        offer_answer_tool=offer_answer_tool,
        repairable=repair is not None,
    )
    try:
        await _drive(run, provider)
        if repair is not None and run.final is not None:
            if (prompt := repair(run.outcome, run.final)) is not None:
                run.begin_repair(prompt)
                await _drive(run, provider)
    except asyncio.CancelledError as error:
        run.cancel()
        raise RunCancelled(run.result()) from error
    return run.result()


async def _drive(run: _Run, provider: Provider) -> None:
    while (request := run.next_request()) is not None:
        try:
            response = await _call(provider, request)
        except ProviderError as error:
            run.fail(error)
        else:
            if response is None:
                run.timed_out()
            else:
                run.respond(response)


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
    offer_answer_tool: bool = True,
    repair: RepairPrompt | None = None,
) -> RunResult:
    """Run `run_assistant_async` from synchronous code; refused inside a running loop."""
    return run_sync(
        "run_assistant",
        lambda: run_assistant_async(
            question,
            provider=provider,
            session=session,
            provider_kind=provider_kind,
            model=model,
            limits=limits,
            registry=registry,
            clock=clock,
            offer_answer_tool=offer_answer_tool,
            repair=repair,
        ),
    )


def run_sync[T](name: str, workflow: Callable[[], Awaitable[T]]) -> T:
    """Run `workflow` to completion for the synchronous wrapper `name` of `{name}_async`."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(workflow())
    raise RunInEventLoopError(name)
