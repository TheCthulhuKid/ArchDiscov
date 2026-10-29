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
    ProviderErrorCode,
)
from archdiscov.assistant.runs import (
    Cancelled,
    LimitReached,
    ProviderFailed,
    RunLimitKind,
    RunLimits,
)
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.session import ToolSession
from tests.support.scripted_turns import SNAPSHOT, HangingProvider, calls_turn, text_turn

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


def test_sync_and_async_entry_points_agree() -> None:
    turns = [calls_turn(("c1", "search_resources", {})), text_turn("Done.")]
    provider, clock = _fake(turns)
    sync = run_assistant(QUESTION, **_kwargs(provider, clock.monotonic))
    asynchronous, _ = _async(turns)
    assert sync == asynchronous
    assert sync.outcome.message.text == "Done."


# ---- deadlines ---------------------------------------------------------------------


def test_provider_timeouts_are_provider_errors() -> None:
    result, _ = _async(
        [{**text_turn(), "delay_seconds": 20}], RunLimits(request_timeout_seconds=10)
    )
    assert isinstance(result.outcome, ProviderFailed)
    assert result.outcome.code == ProviderErrorCode.TIMEOUT
    assert result.transcript.turns[0].latency_seconds == 10


def test_a_late_response_past_the_request_deadline_is_discarded() -> None:
    result, _ = _async(
        [calls_turn(("c1", "search_resources", {}), delay_seconds=20, ignore_request_limits=True)],
        RunLimits(request_timeout_seconds=10),
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.REQUEST_DEADLINE)
    turn = result.transcript.turns[0]
    assert (turn.stop_reason, turn.tool_calls, turn.latency_seconds) == (None, (), 20)
    assert result.returned == ReturnedIds()


def test_the_overall_deadline_stops_further_requests() -> None:
    result, provider = _async(
        [calls_turn(("c1", "search_resources", {}), delay_seconds=10)],
        RunLimits(request_timeout_seconds=10, overall_timeout_seconds=10),
    )
    assert result.outcome == LimitReached(limit=RunLimitKind.OVERALL_DEADLINE)
    assert len(provider.requests) == 1
    assert result.returned != ReturnedIds()


def test_the_overall_deadline_shortens_the_last_request() -> None:
    result, provider = _async(
        [
            calls_turn(("c1", "search_resources", {}), delay_seconds=10),
            {**text_turn(), "delay_seconds": 8},
        ],
        RunLimits(request_timeout_seconds=10, overall_timeout_seconds=15),
    )
    assert provider.requests[1].request.limits.timeout_seconds == 5
    assert result.outcome == LimitReached(limit=RunLimitKind.OVERALL_DEADLINE)
    assert result.transcript.turns[1].error == ProviderErrorCode.TIMEOUT
    assert result.transcript.latency_seconds == 15


def test_a_hanging_provider_is_cut_off_and_settled() -> None:
    provider = HangingProvider()
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
    provider = HangingProvider(answer_first=True)
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
    assert result.returned == session.record.returned()
    assert result.returned != ReturnedIds()
    assert provider.settled is True


# ---- sync wrapper ------------------------------------------------------------------


def test_the_sync_wrapper_refuses_a_running_event_loop() -> None:
    provider, clock = _fake([text_turn()])

    async def inside_a_loop() -> None:
        with pytest.raises(RunInEventLoopError, match="await run_assistant_async"):
            run_assistant(QUESTION, **_kwargs(provider, clock.monotonic))

    asyncio.run(inside_a_loop())
    assert provider.requests == ()
