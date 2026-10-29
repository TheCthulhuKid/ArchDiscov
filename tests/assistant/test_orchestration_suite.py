"""Every run outcome, through fake-provider transcripts, derived from the outcome types.

`EXPECTED` is computed from `RunOutcome`, `RunLimitKind`, `MalformedOutputReason`,
`ProviderErrorCode`, and the answer stop reasons, so a new outcome, limit, reason, or code
fails `test_every_outcome_is_covered` until a case is added below. Each case also runs
through every provider harness, so the same turns pass through each adapter's mapping layer
to the same run; because they do, the per-outcome checks run on the fake alone. A case
marked `fake_only` describes behaviour only the fake can script.
"""

import asyncio
from typing import get_args

import pytest

from archdiscov.assistant.conversation import AssistantMessage, UserMessage
from archdiscov.assistant.fake import FakeClock
from archdiscov.assistant.orchestrator import RunCancelled, RunResult, run_assistant_async
from archdiscov.assistant.provider import ProviderErrorCode
from archdiscov.assistant.runs import (
    Answered,
    MalformedOutputReason,
    RunLimitKind,
    RunLimits,
    RunOutcome,
    RunTranscript,
)
from archdiscov.assistant.tools.registry import ToolRegistry
from archdiscov.assistant.tools.session import ToolSession
from tests.support.orchestration_cases import LOOP_CASES, LOOP_QUESTION, case_capabilities
from tests.support.provider_harness import HARNESSES, FakeHarness, ProviderHarness
from tests.support.scripted_turns import (
    SNAPSHOT,
    HangingProvider,
)
from tests.support.secrets import SECRET


def _outcome_kinds() -> set[str]:
    return {member.model_fields["kind"].default for member in get_args(get_args(RunOutcome)[0])}


def _expected() -> set[tuple[str, str]]:
    stops = get_args(Answered.model_fields["stop_reason"].annotation)
    detail = {
        "answered": {stop.value for stop in stops},
        "limit_reached": {kind.value for kind in RunLimitKind},
        "malformed_output": {reason.value for reason in MalformedOutputReason},
        "provider_error": {
            code.value for code in ProviderErrorCode if code != ProviderErrorCode.MALFORMED_OUTPUT
        },
        "proposed": {""},
        "cancelled": {""},
    }
    assert set(detail) == _outcome_kinds()
    return {(kind, value) for kind, values in detail.items() for value in values}


def _identity(result: RunResult) -> tuple[str, str]:
    outcome = result.outcome
    value = {
        "answered": lambda: outcome.stop_reason.value,
        "limit_reached": lambda: outcome.limit.value,
        "malformed_output": lambda: outcome.reason.value,
        "provider_error": lambda: outcome.code.value,
        "proposed": lambda: "",
        "cancelled": lambda: "",
    }[outcome.kind]()
    return outcome.kind, value


def _run_case(case: dict, harness: ProviderHarness) -> RunResult:
    clock = FakeClock()
    provider = harness.build(case["turns"], capabilities=case_capabilities(case), clock=clock)
    registry = ToolRegistry(case["registry"]) if "registry" in case else None
    offer_answer_tool = case.get("offer_answer_tool", True)
    return asyncio.run(
        run_assistant_async(
            LOOP_QUESTION,
            provider=provider,
            session=ToolSession.from_paths(SNAPSHOT),
            provider_kind=harness.name,
            model="fake",
            limits=RunLimits.model_validate(case["limits"]),
            registry=registry,
            clock=clock.monotonic,
            offer_answer_tool=offer_answer_tool,
            repair=_repair if case.get("repair") else None,
        )
    )


def _repair(outcome: RunOutcome, message: AssistantMessage) -> tuple[UserMessage, ...]:
    return (UserMessage(text="Submit the answer."),)


def _run_cancelled() -> RunResult:
    provider = HangingProvider(answer_first=True)

    async def scenario() -> RunResult:
        task = asyncio.create_task(
            run_assistant_async(
                LOOP_QUESTION,
                provider=provider,
                session=ToolSession.from_paths(SNAPSHOT),
                provider_kind="fake",
                model="fake",
            )
        )
        await provider.entered.wait()
        task.cancel()
        try:
            await task
        except RunCancelled as error:
            return error.result
        raise AssertionError("expected RunCancelled")

    return asyncio.run(scenario())


FAKE = FakeHarness()


def _result(identity: tuple[str, str], harness: ProviderHarness = FAKE) -> RunResult:
    """The case's run; skipped for a harness that cannot script it."""
    if identity == ("cancelled", ""):
        if harness.name != FAKE.name:
            pytest.skip("cancellation is scripted by its own hanging provider")
        return _run_cancelled()
    case = LOOP_CASES[identity]
    if "fake_only" in case and harness.name != FAKE.name:
        pytest.skip(case["fake_only"])
    return _run_case(case, harness)


def _comparable(result: RunResult) -> RunResult:
    """The result without the provider kind, which names the harness."""
    transcript = result.transcript.model_copy(update={"provider_kind": "any"})
    return result.model_copy(update={"transcript": transcript})


def test_every_outcome_is_covered() -> None:
    assert set(LOOP_CASES) | {("cancelled", "")} == _expected()


@pytest.mark.parametrize(
    "identity", sorted(_expected()), ids=lambda item: "-".join(filter(None, item))
)
def test_each_outcome_is_reached_exactly_without_leaking_a_secret(
    identity: tuple[str, str],
) -> None:
    result = _result(identity)
    assert _identity(result) == identity
    transcript = result.transcript
    assert RunTranscript.model_validate_json(transcript.model_dump_json()) == transcript
    assert _identity(result.model_copy(update={"outcome": transcript.outcome})) == identity
    assert len(transcript.turns) <= transcript.limits.max_provider_requests + 1
    assert result.returned == result.returned.model_validate_json(result.returned.model_dump_json())
    for text in (transcript.model_dump_json(), repr(transcript)):
        assert SECRET[:20] not in text


@pytest.mark.parametrize(
    "identity", sorted(LOOP_CASES), ids=lambda item: "-".join(filter(None, item))
)
def test_every_harness_reaches_the_same_run(identity: tuple[str, str]) -> None:
    if "fake_only" in LOOP_CASES[identity]:
        pytest.skip(LOOP_CASES[identity]["fake_only"])
    fake, *adapters = (_comparable(_result(identity, harness)) for harness in HARNESSES)
    for result in adapters:
        assert result == fake


def test_the_answered_result_keeps_the_raw_answer_for_validation() -> None:
    result = _result(("answered", "end"))
    assert result.outcome.message.text == f"Done {SECRET}"
    assert SECRET not in result.transcript.outcome.message.text
