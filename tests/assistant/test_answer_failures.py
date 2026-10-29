"""Explaining failed runs from the outcome and the run record alone.

The answer service suite renders every failure outcome; the cases here show what an
explanation states.
"""

from archdiscov.assistant import answer_rendering
from archdiscov.assistant.answer_rendering import (
    NOT_VALIDATED,
    RETRIEVED_NOT_ANSWER,
    render_failure,
)
from archdiscov.assistant.answer_results import AnswerOutcome
from archdiscov.assistant.provider import PROVIDER_ERROR_MESSAGES, ProviderErrorCode
from archdiscov.assistant.rejections import MAX_LISTED_REJECTIONS, RejectionCode, reject
from archdiscov.assistant.tools.contracts import ToolLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.models.enums import AnalyzerKind, SnapshotSourceKind
from tests.paths import FIXTURES_ROOT
from tests.support.answer_claims import analyzed_findings, dispatch_tool
from tests.support.audit_scenarios import SCENARIOS, with_coverage

OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
CANCELLED = AnswerOutcome(status="failed", failure="cancelled")


def test_a_failure_states_why_the_run_stopped_and_shows_no_answer() -> None:
    code = ProviderErrorCode.TIMEOUT
    outcome = AnswerOutcome(status="failed", failure="provider_error", provider_error=code)
    rendered = render_failure(ToolSession.from_paths(OWNERSHIP_SNAPSHOT), outcome)
    assert rendered.status is None and rendered.narrative is None and not rendered.conclusions
    failure = rendered.failure
    assert failure.reason == outcome.failure and failure.retrieved == ()
    assert failure.unverified == (
        f"The provider failed ({code}): {PROVIDER_ERROR_MESSAGES[code]}",
        NOT_VALIDATED,
        "The run retrieved no analyzer findings.",
    )


def test_a_failure_shows_what_the_run_retrieved_as_unverified() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    dispatch_tool(session, "run_analyzer", kind="ownership")
    before = session.record.context()
    failure = render_failure(session, CANCELLED).failure
    assert session.record.context() == before
    assert RETRIEVED_NOT_ANSWER in failure.unverified
    assert "The run retrieved 1 finding(s) from these analyzers: ownership." in failure.unverified
    (conclusion,) = failure.retrieved
    assert conclusion.findings == tuple(item.view for item in session.record.context().views)


def test_rejections_are_listed_with_their_locations_up_to_a_bound() -> None:
    rejections = tuple(
        reject(RejectionCode.UNKNOWN_ID, f"conclusions[0].citations[0].finding_ids[{number}]")
        for number in range(MAX_LISTED_REJECTIONS + 5)
    )
    outcome = AnswerOutcome(status="failed", failure="rejected_after_repair", rejections=rejections)
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    unverified = render_failure(session, outcome).failure.unverified
    listed = [item for item in unverified if item.startswith("Rejected at ")]
    assert len(listed) == MAX_LISTED_REJECTIONS
    assert listed[0] == (
        "Rejected at conclusions[0].citations[0].finding_ids[0] (answer_unknown_id): "
        "This ID is not recorded in the snapshot for this side."
    )
    assert "5 more rejections are not listed." in unverified


def test_retrieved_findings_are_grouped_and_bounded(monkeypatch) -> None:
    session, delivered = analyzed_findings(AnalyzerKind.SECURITY_GROUP_IMPACT)
    monkeypatch.setattr(answer_rendering, "MAX_CITED_IDS", 5)
    monkeypatch.setattr(answer_rendering, "MAX_RETRIEVED", 2)
    failure = render_failure(session, CANCELLED).failure
    assert [len(item.findings) for item in failure.retrieved] == [5, 5]
    assert f"{len(delivered) - 10} more retrieved findings are not shown." in failure.unverified


def test_retrieved_findings_are_grouped_by_analyzer() -> None:
    scenario = SCENARIOS[AnalyzerKind.INTERNET_EXPOSURE]
    session = ToolSession(scenario.build(), limits=ToolLimits(result_limit=100))
    for kind in (AnalyzerKind.OWNERSHIP, AnalyzerKind.INTERNET_EXPOSURE):
        dispatch_tool(
            session, "run_analyzer", kind=kind.value, limit=100, **SCENARIOS[kind].parameters
        )
    analyzers = {finding.analyzer for finding in session.record.context().findings}
    assert len(analyzers) == 2
    failure = render_failure(session, CANCELLED).failure
    named = [
        {kind for kind in analyzers if any(f"the {kind} analyzer" in s for s in item.statements)}
        for item in failure.retrieved
    ]
    assert len(named) == len(analyzers) and all(len(item) == 1 for item in named)


def test_coverage_that_did_not_succeed_is_stated() -> None:
    scenario = SCENARIOS[AnalyzerKind.OWNERSHIP]
    session = ToolSession(with_coverage(scenario.build(), "failed"))
    operations = len(session.index.coverage_records())
    unverified = render_failure(session, CANCELLED).failure.unverified
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
    rendered = render_failure(session, CANCELLED)
    assert rendered.snapshots[0].declared
    assert rendered.qualifications == (
        "The primary snapshot is a Terraform plan: planned changes, not deployed state.",
    )
