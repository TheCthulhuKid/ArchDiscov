import asyncio
from typing import Any

import pytest

from archdiscov.assistant.fake import FakeClock, FakeProvider, FakeTranscript
from archdiscov.assistant.orchestrator import (
    RunCancelled,
    RunInEventLoopError,
    run_assistant,
    run_assistant_async,
)
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderCapabilities,
    ProviderCapability,
    ProviderErrorCode,
)
from archdiscov.assistant.runs import (
    Answered,
    Cancelled,
    LimitReached,
    ProviderFailed,
    RunLimitKind,
    RunLimits,
)
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.session import ToolSession
from tests.assistant.test_orchestrator import SNAPSHOT, _calls, _fail, _text

QUESTION = "Which resources are in the network?"


def _fake(turns: list[dict]) -> tuple[FakeProvider, FakeClock]:
    clock = FakeClock()
    return FakeProvider(FakeTranscript.model_validate({"turns": turns}), clock=clock), clock


def _kwargs(provider, clock, limits: RunLimits | None = None) -> dict[str, Any]:
    return {
        "provider": provider,
        "session": ToolSession.from_paths(SNAPSHOT),
        "provider_kind": "fake",
        "model": "fake",
        "limits": limits,
        "clock": clock,
    }


def _async(turns: list[dict], limits: RunLimits | None = None):
    provider, clock = _fake(turns)
    result = asyncio.run(
        run_assistant_async(QUESTION, **_kwargs(provider, clock.monotonic, limits))
    )
    return result, provider


# ---- sync and async agree ----------------------------------------------------------

CASES = {
    "answer": ([_text()], None),
    "tools": ([_calls(("c1", "search_resources", {})), _text()], None),
    "parallel": (
        [_calls(("c1", "search_resources", {}), ("c2", "list_analyzers", {})), _text()],
        RunLimits(max_tool_calls_per_turn=2),
    ),
    "request-limit": (
        [_calls(("c1", "search_resources", {})), _calls(("c2", "search_resources", {}))],
        RunLimits(max_provider_requests=2),
    ),
    "provider-error": ([_fail("provider_refusal")], None),
    "provider-timeout": ([{**_text(), "delay_seconds": 70}], None),
}


@pytest.mark.parametrize("case", list(CASES))
def test_sync_and_async_entry_points_agree(case: str) -> None:
    turns, limits = CASES[case]
    provider, clock = _fake(turns)
    sync = run_assistant(QUESTION, **_kwargs(provider, clock.monotonic, limits))
    asynchronous, _ = _async(turns, limits)
    assert sync == asynchronous


# ---- deadlines ---------------------------------------------------------------------


def test_provider_timeouts_are_provider_errors() -> None:
    result, _ = _async([{**_text(), "delay_seconds": 20}], RunLimits(request_timeout_seconds=10))
    assert isinstance(result.outcome, ProviderFailed)
    assert result.outcome.code == ProviderErrorCode.TIMEOUT
    assert result.transcript.turns[0].latency_seconds == 10


def test_a_late_response_past_the_request_deadline_is_discarded() -> None:
    result, _ = _async(
        [_calls(("c1", "search_resources", {}), delay_seconds=20, ignore_request_limits=True)],
        RunLimits(request_timeout_seconds=10),
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.REQUEST_DEADLINE)
    turn = result.transcript.turns[0]
    assert (turn.stop_reason, turn.tool_calls, turn.latency_seconds) == (None, (), 20)
    assert result.returned == ReturnedIds()


def test_the_overall_deadline_stops_further_requests() -> None:
    result, provider = _async(
        [_calls(("c1", "search_resources", {}), delay_seconds=10)],
        RunLimits(request_timeout_seconds=10, overall_timeout_seconds=10),
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.OVERALL_DEADLINE)
    assert len(provider.requests) == 1
    assert result.returned != ReturnedIds()


def test_the_overall_deadline_shortens_the_last_request() -> None:
    result, provider = _async(
        [
            _calls(("c1", "search_resources", {}), delay_seconds=10),
            {**_text(), "delay_seconds": 8},
        ],
        RunLimits(request_timeout_seconds=10, overall_timeout_seconds=15),
    )
    assert provider.requests[1].request.limits.timeout_seconds == 5
    assert result.outcome == LimitReached(limit=RunLimitKind.OVERALL_DEADLINE)
    assert result.transcript.turns[1].error == ProviderErrorCode.TIMEOUT
    assert result.transcript.latency_seconds == 15


def test_responses_within_the_deadlines_are_kept() -> None:
    result, _ = _async(
        [_calls(("c1", "search_resources", {}), delay_seconds=9), {**_text(), "delay_seconds": 9}],
        RunLimits(request_timeout_seconds=10, overall_timeout_seconds=20),
    )
    assert isinstance(result.outcome, Answered)
    assert result.transcript.latency_seconds == 18


class _HangingProvider:
    """Never answers in time; resists the first cancellation, like a call in a thread."""

    def __init__(self, *, answer_first: bool = False) -> None:
        self.answer_first = answer_first
        self.calls = 0
        self.entered = asyncio.Event()
        self.settled = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(capabilities={ProviderCapability.TOOL_CALLING})

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise AssertionError("the loop calls providers asynchronously")

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        if self.answer_first and self.calls == 1:
            return CompletionResponse.model_validate(
                _calls(("c1", "search_resources", {}))["response"]
            )
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)  # finish the work in progress, then stop
            self.settled = True
            raise
        raise AssertionError("unreachable")


def test_a_hanging_provider_is_cut_off_and_settled() -> None:
    provider = _HangingProvider()
    limits = RunLimits(request_timeout_seconds=0.05, overall_timeout_seconds=1)
    result = asyncio.run(
        run_assistant_async(
            QUESTION,
            provider=provider,
            session=ToolSession.from_paths(SNAPSHOT),
            provider_kind="fake",
            model="fake",
            limits=limits,
        )
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.REQUEST_DEADLINE)
    assert provider.settled is True
    assert len(result.transcript.turns) == 1


# ---- cancellation ------------------------------------------------------------------


def test_cancellation_ends_the_run_with_a_partial_transcript() -> None:
    provider = _HangingProvider(answer_first=True)
    session = ToolSession.from_paths(SNAPSHOT)

    async def scenario() -> RunCancelled:
        task = asyncio.create_task(
            run_assistant_async(
                QUESTION, provider=provider, session=session, provider_kind="fake", model="fake"
            )
        )
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert task.cancelled()
        return raised.value

    error = asyncio.run(scenario())
    assert isinstance(error, RunCancelled)
    result = error.result
    assert result.outcome == Cancelled()
    assert result.transcript.outcome == Cancelled()
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [1, 0]
    assert result.transcript.turns[1].stop_reason is None
    assert result.returned == session.record.returned() != ReturnedIds()
    assert provider.settled is True


# ---- sync wrapper ------------------------------------------------------------------


def test_the_sync_wrapper_refuses_a_running_event_loop() -> None:
    provider, clock = _fake([_text()])

    async def inside_a_loop() -> None:
        with pytest.raises(RunInEventLoopError, match="await run_assistant_async"):
            run_assistant(QUESTION, **_kwargs(provider, clock.monotonic))

    asyncio.run(inside_a_loop())
    assert provider.requests == ()


def test_the_sync_wrapper_returns_the_async_result() -> None:
    provider, clock = _fake([_text("Done.")])
    result = run_assistant(QUESTION, **_kwargs(provider, clock.monotonic))
    assert isinstance(result.outcome, Answered)
    assert result.outcome.message.text == "Done."
