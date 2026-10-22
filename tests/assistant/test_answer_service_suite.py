"""Every answer outcome, end to end through `answer_question`, derived from the outcome types.

`EXPECTED` is computed from `AnswerStatus`, `FailureReason`, `MalformedOutputReason`,
`ProviderErrorCode`, and `RunLimitKind`, so a new status, reason, or detail fails
`test_every_answer_outcome_is_covered` until a case is added. Unrepairable outcomes reuse
the loop's cases from the orchestration suite; repairable ones script both attempts here.
Every case checks the rendering, the exit code, the number of repair attempts, and that no
secret or fabricated narrative claim appears outside the labelled narrative.
"""

import asyncio
import json
from typing import Any

import pytest

from archdiscov.assistant.answer_results import (
    FAILURE_EXIT_CODES,
    SUCCESS_EXIT_CODE,
    AnswerOutcome,
    AnswerStatus,
    FailureReason,
)
from archdiscov.assistant.answer_service import (
    AnswerCancelled,
    AnswerResult,
    answer_question_async,
)
from archdiscov.assistant.fake import FakeClock
from archdiscov.assistant.provider import ProviderErrorCode
from archdiscov.assistant.runs import MalformedOutputReason, RunLimitKind, RunLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.cli_answer_output import answer_data, answer_text
from tests.assistant.provider_harness import HARNESSES, FakeHarness, ProviderHarness
from tests.assistant.test_answer_service import PLAIN_TEXT, _proposal, _rejected, _submit
from tests.assistant.test_orchestration_suite import CASES as LOOP_CASES
from tests.assistant.test_orchestration_suite import QUESTION, SEARCH, SECRET, capabilities
from tests.assistant.test_orchestrator import SNAPSHOT, _calls, _text
from tests.assistant.test_orchestrator_async import _HangingProvider
from tests.assistant.test_secret_leaks import _renderings

FABRICATED = "Every database in the network is publicly reachable and unencrypted."
# `submit_answer` is always offered by the answer service, so a run never has no tools.
UNREACHABLE = {("failed", "malformed_output", MalformedOutputReason.TOOL_CALLS_WITHOUT_TOOLS)}
# The repaired attempt failing again ends in these; everything else gets no repair.
REPAIRED = {
    ("answered_after_repair", "", ""),
    ("failed", "rejected_after_repair", ""),
    ("failed", "no_proposal_after_repair", ""),
    *(
        ("failed", "malformed_output", reason.value)
        for reason in (
            MalformedOutputReason.PROPOSAL_MALFORMED,
            MalformedOutputReason.PROPOSAL_INVALID,
            MalformedOutputReason.PROPOSAL_MIXED,
            MalformedOutputReason.TOOL_NOT_OFFERED,
        )
    ),
}

type Identity = tuple[str, str, str]


def _expected() -> set[Identity]:
    details = {
        FailureReason.REJECTED_AFTER_REPAIR: {""},
        FailureReason.NO_PROPOSAL_AFTER_REPAIR: {""},
        FailureReason.MALFORMED_OUTPUT: {reason.value for reason in MalformedOutputReason},
        FailureReason.PROVIDER_ERROR: {
            code.value for code in ProviderErrorCode if code != ProviderErrorCode.MALFORMED_OUTPUT
        },
        FailureReason.LIMIT_REACHED: {kind.value for kind in RunLimitKind},
        FailureReason.CANCELLED: {""},
    }
    assert set(details) == set(FailureReason)
    successes = {(status.value, "", "") for status in AnswerStatus if status != AnswerStatus.FAILED}
    failures = {
        ("failed", reason.value, detail) for reason, values in details.items() for detail in values
    }
    return (successes | failures) - UNREACHABLE


def _identity(outcome: AnswerOutcome) -> Identity:
    detail = outcome.malformed or outcome.provider_error or outcome.limit or ""
    return outcome.status.value, outcome.failure or "", str(detail)


def _fabricated(session: ToolSession) -> dict:
    return _proposal(session, summary=f"{FABRICATED} {SECRET}", explanation=FABRICATED)


def _mixed(call_id: str, session: ToolSession) -> dict:
    search = (f"s{call_id}", "search_resources", {"name": SECRET})
    return _calls(
        (call_id, "submit_answer", _fabricated(session)), search, ignore_request_limits=True
    )


def _case(turns, limits: dict | None = None, **options: Any) -> dict:
    return {"turns": turns, "limits": limits or {}, **options}


def _twice(attempt) -> dict:
    return _case(lambda session: [attempt("a", session), attempt("b", session)])


SCRIPTED: dict[Identity, dict] = {
    ("answered", "", ""): _case(
        lambda session: [_calls(SEARCH), _submit("a", _fabricated(session))]
    ),
    ("answered_after_repair", "", ""): _case(
        lambda session: [_submit("a", _rejected(session)), _submit("b", _fabricated(session))]
    ),
    ("failed", "rejected_after_repair", ""): _twice(
        lambda call, session: _submit(call, _rejected(session))
    ),
    ("failed", "no_proposal_after_repair", ""): _twice(lambda call, _: _text(PLAIN_TEXT)),
    ("failed", "malformed_output", "proposal_malformed"): _twice(
        lambda call, _: _submit(call, None)
    ),
    ("failed", "malformed_output", "proposal_invalid"): _twice(
        lambda call, _: _submit(call, {"status": "supported", "summary": SECRET})
    ),
    ("failed", "malformed_output", "proposal_mixed"): _twice(_mixed),
    ("failed", "malformed_output", "tool_not_offered"): _case(
        lambda _: [_text(PLAIN_TEXT), _calls(SEARCH)]
    ),
}

# The loop's own cases for outcomes no repair can follow.
_LOOP_MALFORMED = {
    ("malformed_output", "conversation_invalid"),
    ("malformed_output", "provider_reported"),
    ("malformed_output", "continuation_foreign"),
}


def _from_loop(case: dict) -> dict:
    options = {key: case[key] for key in ("capabilities", "fake_only") if key in case}
    return _case(lambda _: case["turns"], case["limits"], **options)


FROM_LOOP: dict[Identity, dict] = {
    ("failed", kind, value): _from_loop(case)
    for (kind, value), case in LOOP_CASES.items()
    if kind in {"limit_reached", "provider_error"} or (kind, value) in _LOOP_MALFORMED
}

CASES = {**SCRIPTED, **FROM_LOOP}


def _run(case: dict, harness: ProviderHarness) -> tuple[AnswerResult, int]:
    session = ToolSession.from_paths(SNAPSHOT)
    clock = FakeClock()
    provider = harness.build(case["turns"](session), capabilities=capabilities(case), clock=clock)
    result = asyncio.run(
        answer_question_async(
            QUESTION,
            provider=provider,
            session=session,
            provider_kind=harness.name,
            model="fake",
            limits=RunLimits.model_validate(case["limits"]),
            clock=clock.monotonic,
        )
    )
    return result, harness.attempts(provider)


def _cancelled() -> tuple[AnswerResult, int]:
    provider = _HangingProvider(answer_first=True)

    async def scenario() -> AnswerResult:
        task = asyncio.create_task(
            answer_question_async(
                QUESTION,
                provider=provider,
                session=ToolSession.from_paths(SNAPSHOT),
                provider_kind="fake",
                model="fake",
            )
        )
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(AnswerCancelled) as raised:
            await task
        return raised.value.result

    return asyncio.run(scenario()), provider.calls


FAKE = FakeHarness()


def _result(identity: Identity, harness: ProviderHarness = FAKE) -> tuple[AnswerResult, int]:
    """The case's answer and provider attempts; skipped for a harness that cannot script it."""
    if identity == ("failed", "cancelled", ""):
        if harness.name != FAKE.name:
            pytest.skip("cancellation is scripted by its own hanging provider")
        return _cancelled()
    case = CASES[identity]
    if "fake_only" in case and harness.name != FAKE.name:
        pytest.skip(case["fake_only"])
    return _run(case, harness)


def test_every_answer_outcome_is_covered() -> None:
    assert set(CASES) | {("failed", "cancelled", "")} == _expected()
    assert REPAIRED <= _expected()


@pytest.mark.parametrize(
    "identity", sorted(_expected()), ids=lambda item: "-".join(filter(None, item))
)
def test_each_answer_outcome_is_reached_and_rendered(identity: Identity, harness) -> None:
    result, requests = _result(identity, harness)
    outcome = result.outcome
    assert _identity(outcome) == identity
    exit_code = (
        SUCCESS_EXIT_CODE if outcome.failure is None else FAILURE_EXIT_CODES[outcome.failure]
    )
    assert outcome.exit_code == exit_code == answer_data(result.rendered)["exit_code"]
    turns = result.transcript.turns
    # A capability failure is recorded as a turn but never reaches the provider.
    reached = [turn for turn in turns if turn.error != ProviderErrorCode.UNSUPPORTED_CAPABILITY]
    assert requests == len(reached)
    repairs = [turn for turn in turns if turn.repair]
    assert len(repairs) == (1 if identity in REPAIRED else 0)
    assert (result.transcript.repair is not None) == (identity in REPAIRED)
    rendered = result.rendered
    assert rendered.outcome == outcome
    first = answer_text(rendered).splitlines()[0]
    if outcome.failure is None:
        assert rendered.failure is None and rendered.narrative is not None
        assert first.startswith("Answer: answered")
    else:
        assert rendered.failure.reason == outcome.failure and rendered.narrative is None
        assert first == f"Answer: failed ({outcome.failure}), exit code {exit_code}"
    assert AnswerResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "identity", sorted(_expected()), ids=lambda item: "-".join(filter(None, item))
)
def test_no_secret_or_fabricated_claim_leaks(identity: Identity, harness) -> None:
    result, _ = _result(identity, harness)
    text = answer_text(result.rendered)
    data = json.dumps(answer_data(result.rendered))
    for rendering in [*_renderings(result), text, data]:
        assert SECRET not in rendering and SECRET[:20] not in rendering
    narrative = [line for line in text.splitlines() if line.startswith("  |")]
    outside_narrative = [line for line in text.splitlines() if not line.startswith("  |")]
    assert all(FABRICATED not in line for line in outside_narrative)
    if result.outcome.failure is None:
        # The claim was submitted, so it must show, but only as the labelled narrative.
        assert any(FABRICATED in line for line in narrative)
    rendered = result.rendered
    conclusions = [*rendered.conclusions, *(rendered.failure.retrieved if rendered.failure else ())]
    for conclusion in conclusions:
        assert all(FABRICATED not in item for item in conclusion.statements)
        assert all(FABRICATED not in item.untrusted_text for item in conclusion.data)
    assert all(FABRICATED not in item for item in rendered.qualifications)


@pytest.mark.parametrize("identity", sorted(CASES), ids=lambda item: "-".join(filter(None, item)))
def test_every_harness_reaches_the_same_answer(identity: Identity) -> None:
    if "fake_only" in CASES[identity]:
        pytest.skip(CASES[identity]["fake_only"])
    results = [_result(identity, harness)[0] for harness in HARNESSES]
    fake, *adapters = (
        result.model_copy(
            update={"transcript": result.transcript.model_copy(update={"provider_kind": "any"})}
        )
        for result in results
    )
    for result in adapters:
        assert result == fake
