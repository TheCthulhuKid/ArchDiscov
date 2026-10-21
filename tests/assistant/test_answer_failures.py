"""Explaining failed runs from the outcome and the run record alone.

Outcomes come from every run outcome after its repair, and from every provider error code,
so a new failure reason, malformed reason, limit, or code fails here until it is explained.
"""

import pytest

from archdiscov.assistant import answer_rendering
from archdiscov.assistant.answer_rendering import (
    NOT_VALIDATED,
    RETRIEVED_NOT_ANSWER,
    render_failure,
)
from archdiscov.assistant.answer_results import (
    AnswerOutcome,
    FailureReason,
    RenderedAnswer,
    final_outcome,
)
from archdiscov.assistant.answer_validation import validate_proposal
from archdiscov.assistant.fake import FakeProvider, FakeTranscript
from archdiscov.assistant.orchestrator import run_assistant
from archdiscov.assistant.provider import PROVIDER_ERROR_MESSAGES, ProviderErrorCode
from archdiscov.assistant.rejections import MAX_LISTED_REJECTIONS, RejectionCode, reject
from archdiscov.assistant.runs import MalformedOutputReason, Proposed, RunLimitKind
from archdiscov.assistant.tools.contracts import ToolLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.models.enums import AnalyzerKind, SnapshotSourceKind
from archdiscov.services.snapshots import load_snapshot
from tests.assistant.test_answer_claims import _analysis, _call
from tests.assistant.test_answer_results import REJECTIONS, _run_outcomes
from tests.assistant.test_answer_suite import INJECTION, SECRET, _answer, _proposal
from tests.paths import FIXTURES_ROOT
from tests.support.audit_scenarios import SCENARIOS, with_coverage

OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"


def _failures() -> list[AnswerOutcome]:
    """A failed answer outcome for every run outcome and every provider error code."""
    outcomes = [
        final_outcome(item, REJECTIONS if isinstance(item, Proposed) else (), repaired=True)
        for item in _run_outcomes()
    ]
    outcomes += [
        AnswerOutcome(status="failed", failure="provider_error", provider_error=code)
        for code in ProviderErrorCode
    ]
    return outcomes


FAILURES = _failures()


def _id(outcome: AnswerOutcome) -> str:
    detail = outcome.malformed or outcome.provider_error or outcome.limit or ""
    return f"{outcome.failure}-{detail}"


def test_examples_cover_every_failure_reason() -> None:
    assert {item.failure for item in FAILURES} == set(FailureReason)


def _session(*, retrieve: bool) -> ToolSession:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    if retrieve:
        _call(session, "run_analyzer", kind="ownership")
    return session


@pytest.mark.parametrize("retrieve", [False, True], ids=["nothing-retrieved", "retrieved"])
@pytest.mark.parametrize("outcome", FAILURES, ids=_id)
def test_every_failure_is_explained(outcome: AnswerOutcome, retrieve: bool) -> None:
    session = _session(retrieve=retrieve)
    rendered = render_failure(session, outcome)
    assert RenderedAnswer.model_validate_json(rendered.model_dump_json()) == rendered
    failure = rendered.failure
    assert failure.reason == outcome.failure
    assert rendered.status is None and rendered.narrative is None and not rendered.conclusions
    assert NOT_VALIDATED in failure.unverified
    if retrieve:
        assert RETRIEVED_NOT_ANSWER in failure.unverified
        assert "The run retrieved 1 finding(s) from these analyzers: ownership." in (
            failure.unverified
        )
        (conclusion,) = failure.retrieved
        context = session.record.context()
        assert conclusion.findings == tuple(item.view for item in context.views)
    else:
        assert failure.retrieved == ()
        assert "The run retrieved no analyzer findings." in failure.unverified
        assert RETRIEVED_NOT_ANSWER not in failure.unverified


@pytest.mark.parametrize("code", list(ProviderErrorCode))
def test_provider_errors_are_stated_with_their_fixed_message(code: ProviderErrorCode) -> None:
    outcome = AnswerOutcome(status="failed", failure="provider_error", provider_error=code)
    first = render_failure(_session(retrieve=False), outcome).failure.unverified[0]
    assert first == f"The provider failed ({code}): {PROVIDER_ERROR_MESSAGES[code]}"


def test_each_limit_and_malformed_reason_has_its_own_statement() -> None:
    for field, values in (("limit", RunLimitKind), ("malformed", MalformedOutputReason)):
        failure = "limit_reached" if field == "limit" else "malformed_output"
        stated = {
            render_failure(
                _session(retrieve=False),
                AnswerOutcome(status="failed", failure=failure, **{field: value}),
            ).failure.unverified[0]
            for value in values
        }
        assert len(stated) == len(values)


def test_rejections_are_listed_with_their_locations_up_to_a_bound() -> None:
    rejections = tuple(
        reject(RejectionCode.UNKNOWN_ID, f"conclusions[0].citations[0].finding_ids[{number}]")
        for number in range(MAX_LISTED_REJECTIONS + 5)
    )
    outcome = AnswerOutcome(status="failed", failure="rejected_after_repair", rejections=rejections)
    unverified = render_failure(_session(retrieve=False), outcome).failure.unverified
    listed = [item for item in unverified if item.startswith("Rejected at ")]
    assert len(listed) == MAX_LISTED_REJECTIONS
    assert listed[0] == (
        "Rejected at conclusions[0].citations[0].finding_ids[0] (answer_unknown_id): "
        "This ID is not recorded in the snapshot for this side."
    )
    assert "5 more rejections are not listed." in unverified


def test_a_rejected_run_never_shows_its_narrative_or_secrets() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    finding = _call(session, "run_analyzer", kind="ownership")["findings"]["items"][0]
    # Overstated, so validation rejects it; the narrative carries a secret and an instruction.
    proposal = _proposal(
        session,
        [],
        status="supported",
        summary=f"{INJECTION} {SECRET}",
        explanation=f"Fabricated claim {SECRET}.",
        conclusions=[
            {
                "kind": "analyzer_finding",
                "citations": [{"finding_ids": [finding["finding_id"], "invented"]}],
            }
        ],
    )
    run = _answer(session, [], proposal)
    rejections = validate_proposal(run.outcome.proposal, session)
    assert rejections
    rendered = render_failure(session, final_outcome(run.outcome, rejections, repaired=True))
    text = rendered.model_dump_json()
    for leaked in (SECRET, INJECTION, "Fabricated claim"):
        assert leaked not in text
    assert any(item.startswith("Rejected at ") for item in rendered.failure.unverified)


def test_explaining_a_failure_makes_no_provider_call() -> None:
    session = _session(retrieve=False)
    provider = FakeProvider(
        FakeTranscript.model_validate({"turns": [{"kind": "fail", "error": "provider_timeout"}]})
    )
    run = run_assistant("q", provider=provider, session=session, provider_kind="fake", model="f")
    made = len(provider.requests)
    before = session.record.context()
    render_failure(session, final_outcome(run.outcome, (), repaired=True))
    assert len(provider.requests) == made
    assert session.record.context() == before


def test_retrieved_findings_are_grouped_and_bounded(monkeypatch) -> None:
    session, delivered = _analysis(AnalyzerKind.SECURITY_GROUP_IMPACT)
    monkeypatch.setattr(answer_rendering, "MAX_CITED_IDS", 5)
    monkeypatch.setattr(answer_rendering, "MAX_RETRIEVED", 2)
    failure = render_failure(session, AnswerOutcome(status="failed", failure="cancelled")).failure
    assert [len(item.findings) for item in failure.retrieved] == [5, 5]
    assert f"{len(delivered) - 10} more retrieved findings are not shown." in failure.unverified


def test_retrieved_findings_are_grouped_by_analyzer() -> None:
    scenario = SCENARIOS[AnalyzerKind.INTERNET_EXPOSURE]
    session = ToolSession(scenario.build(), limits=ToolLimits(result_limit=100))
    for kind in (AnalyzerKind.OWNERSHIP, AnalyzerKind.INTERNET_EXPOSURE):
        _call(session, "run_analyzer", kind=kind.value, limit=100, **SCENARIOS[kind].parameters)
    analyzers = {finding.analyzer for finding in session.record.context().findings}
    assert len(analyzers) == 2
    failure = render_failure(session, AnswerOutcome(status="failed", failure="cancelled")).failure
    named = [
        {kind for kind in analyzers if any(f"the {kind} analyzer" in s for s in item.statements)}
        for item in failure.retrieved
    ]
    assert len(named) == len(analyzers) and all(len(item) == 1 for item in named)


def test_coverage_that_did_not_succeed_is_stated() -> None:
    scenario = SCENARIOS[AnalyzerKind.OWNERSHIP]
    session = ToolSession(with_coverage(scenario.build(), "failed"))
    operations = len(session.index.coverage_records())
    unverified = render_failure(
        session, AnswerOutcome(status="failed", failure="cancelled")
    ).failure.unverified
    assert unverified[-1] == (
        f"The primary snapshot records collection that did not succeed for {operations} "
        f"operation(s): {operations} failed."
    )


def test_declared_snapshots_are_qualified_in_failures_too() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    session.record.record_snapshot(
        session.index.snapshot_id,
        SnapshotSourceKind.TERRAFORM_PLAN,
        session.index.collection_window,
    )
    rendered = render_failure(session, AnswerOutcome(status="failed", failure="cancelled"))
    assert rendered.snapshots[0].declared
    assert rendered.qualifications == (
        "The primary snapshot is a Terraform plan: planned changes, not deployed state.",
    )


def test_only_failed_runs_are_explained_as_failures() -> None:
    with pytest.raises(ValueError, match="only a failed run"):
        render_failure(_session(retrieve=False), AnswerOutcome(status="answered"))


def test_a_snapshot_compared_with_itself_shows_its_findings_once() -> None:
    snapshot = load_snapshot(OWNERSHIP_SNAPSHOT)
    session = ToolSession(snapshot, comparison=snapshot)
    _call(session, "run_analyzer", kind="ownership")
    failure = render_failure(session, AnswerOutcome(status="failed", failure="cancelled")).failure
    (conclusion,) = failure.retrieved
    assert conclusion.citations[0].side == "primary"
    assert not any("more retrieved" in item for item in failure.unverified)


@pytest.mark.parametrize("outcome", FAILURES, ids=_id)
def test_failures_never_claim_a_repeated_mistake(outcome: AnswerOutcome) -> None:
    # The repaired attempt may fail differently from the first, so neither is called a repeat.
    unverified = render_failure(_session(retrieve=False), outcome).failure.unverified
    assert not any(" again" in item for item in unverified)
