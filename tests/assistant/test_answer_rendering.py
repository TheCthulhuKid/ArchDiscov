"""Rendering validated proposals: every fact comes from records, in fixed wording.

Each case validates its proposal first, as the answer service will, then checks that the
rendered conclusions show the delivered records and that model or imported text appears
only as the labelled narrative or as quoted data.
"""

import json
from pathlib import Path

import pytest

from archdiscov.assistant.answer_rendering import (
    INSUFFICIENT_REASONS,
    LIMITED_COVERAGE,
    MISSING_DIFFERENCE,
    NO_COMPARISON_ENTRY,
    PARTIAL_ANSWER,
    QUALIFIED_FINDING,
    RECORDED_DIFFERENCES,
    render_answer,
)
from archdiscov.assistant.answer_results import (
    MAX_RENDERED_ENTRIES,
    AnswerOutcome,
    RenderedAnswer,
    RenderedNarrative,
)
from archdiscov.assistant.answer_validation import validate_proposal
from archdiscov.assistant.answers import (
    DECLARED_SOURCE_KINDS,
    AnswerProposal,
    InsufficientReason,
    combined_status,
)
from archdiscov.assistant.redaction import REDACTED
from archdiscov.assistant.tools.contracts import IdKind, ToolLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.assistant.tools.views import FindingView, evidence_view
from archdiscov.models.enums import AnalyzerKind, SnapshotSourceKind
from archdiscov.services.comparison import compare_snapshots
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.answer_claims import (
    INJECTION,
    analyzed_findings,
    cited_proposal,
    dispatch_tool,
    expected_status,
)
from tests.support.ask_examples import renamed_snapshots
from tests.support.secrets import SECRET
from tests.support.terraform_data import FIXTURE as PLAN
from tests.support.terraform_data import collect

SNAPSHOTS = FIXTURES_ROOT / "snapshots"
OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
ANSWERED = AnswerOutcome(status="answered")
FABRICATED = "The database is publicly reachable on port 5432 from 0.0.0.0/0 and unencrypted."


def _render(proposal: AnswerProposal, session: ToolSession) -> RenderedAnswer:
    assert validate_proposal(proposal, session) == ()
    return render_answer(proposal, session, ANSWERED)


def _wording(rendered: RenderedAnswer) -> str:
    """Every fixed statement, the text a model must not be able to word."""
    statements = [item for conclusion in rendered.conclusions for item in conclusion.statements]
    return "\n".join([*statements, *rendered.qualifications])


def _quoted(rendered: RenderedAnswer) -> list[str]:
    return [item.untrusted_text for conclusion in rendered.conclusions for item in conclusion.data]


def _configuration(session: ToolSession, resource: str) -> dict:
    """A configuration conclusion citing every evidence item an inspection returned."""
    inspected = dispatch_tool(session, "inspect_resource", resource_id=resource)
    evidence = [item["evidence_id"] for item in inspected["evidence"]["items"]]
    return {
        "kind": "recorded_configuration",
        "citations": [{"evidence_ids": evidence, "resource_ids": [resource]}],
    }


# ---- analyzer findings -------------------------------------------------------------


@pytest.mark.parametrize("kind", list(AnalyzerKind))
@pytest.mark.parametrize("coverage", [None, "failed"])
def test_every_analyzer_renders_the_views_it_delivered(
    kind: AnalyzerKind, coverage: str | None
) -> None:
    session, delivered = analyzed_findings(kind, coverage)
    status = expected_status(delivered)
    rendered = _render(cited_proposal(session, delivered, status=status), session)
    (conclusion,) = rendered.conclusions
    views = tuple(FindingView.model_validate(item) for item in delivered)
    assert conclusion.findings == views
    context = session.record.context()
    assert views == tuple(
        context.view(session.index.snapshot_id, view.finding_id) for view in views
    )
    assert conclusion.status == combined_status(view.status for view in views)
    assert rendered.status == status
    assert [item.untrusted_text for item in conclusion.data] == [
        view.summary.untrusted_text for view in views
    ]
    assert len(conclusion.statements) == len(views)
    for statement, view in zip(conclusion.statements, views, strict=True):
        assert view.summary.untrusted_text not in statement
        assert kind.value in statement
    qualified = any(view.status != "supported" for view in views)
    assert (QUALIFIED_FINDING in rendered.qualifications) == qualified
    assert (PARTIAL_ANSWER in rendered.qualifications) == (status == "partial")
    coverage_limited = any(item.service for view in views for item in view.limitations)
    assert (LIMITED_COVERAGE in rendered.qualifications) == coverage_limited


def test_failed_coverage_qualifies_the_answer() -> None:
    session, delivered = analyzed_findings(AnalyzerKind.OWNERSHIP, "failed")
    rendered = _render(cited_proposal(session, delivered, status="partial"), session)
    for qualification in (PARTIAL_ANSWER, QUALIFIED_FINDING, LIMITED_COVERAGE):
        assert qualification in rendered.qualifications


def test_insufficient_findings_are_worded_as_what_was_checked() -> None:
    session, delivered = analyzed_findings(AnalyzerKind.DATABASE_ACCESS, "failed")
    rendered = _render(cited_proposal(session, delivered, status="insufficient_evidence"), session)
    statements = rendered.conclusions[0].statements
    assert all("found insufficient evidence to conclude" in item for item in statements)
    reason = INSUFFICIENT_REASONS[InsufficientReason.NO_APPLICABLE_FINDINGS]
    assert reason in rendered.qualifications


# ---- insufficient answers ----------------------------------------------------------


@pytest.mark.parametrize("reason", list(InsufficientReason))
def test_insufficient_answers_say_what_was_checked(reason: InsufficientReason) -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    empty = cited_proposal(session, [], status="insufficient_evidence", insufficient_reason=reason)
    rendered = _render(empty, session)
    assert rendered.conclusions == ()
    assert "The run retrieved no analyzer findings." in rendered.qualifications
    dispatch_tool(session, "run_analyzer", kind="ownership")
    rendered = _render(empty, session)
    assert rendered.qualifications == (
        INSUFFICIENT_REASONS[reason],
        "The run retrieved 1 finding(s) from these analyzers: ownership.",
    )


# ---- configuration -----------------------------------------------------------------


def test_configuration_renders_cited_evidence_and_resources() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    resource = session.index.resource_ids[0]
    conclusion = _configuration(session, resource)
    proposal = cited_proposal(session, [], status="insufficient_evidence", conclusions=[conclusion])
    (rendered,) = _render(proposal, session).conclusions
    evidence = conclusion["citations"][0]["evidence_ids"]
    assert rendered.findings == () and rendered.status is None
    assert len(rendered.statements) == len(evidence) + 1
    quoted = [item.untrusted_text for item in rendered.data]
    for evidence_id in evidence:
        view = evidence_view(evidence_id, session.index.get_evidence(evidence_id))
        assert view.configuration_path.untrusted_text in quoted
        if view.value is not None:
            assert view.value.untrusted_text in quoted
    assert rendered.statements[-1].startswith("Resource 1 on the primary snapshot is recorded as")
    assert rendered.citations == proposal.conclusions[0].citations


def test_planned_resources_and_declared_snapshots_are_shown_as_such() -> None:
    session = ToolSession(collect(PLAN))
    planned = next(
        resource
        for resource in session.index.resource_ids
        if session.index.get_resource(resource).lifecycle is not None
    )
    proposal = cited_proposal(
        session,
        [],
        status="insufficient_evidence",
        conclusions=[_configuration(session, planned)],
    )
    rendered = _render(proposal, session)
    (snapshot,) = rendered.snapshots
    assert snapshot.declared
    assert (
        "The primary snapshot is a Terraform plan: planned changes, not deployed state."
        in rendered.qualifications
    )
    actions = ", ".join(session.index.get_resource(planned).lifecycle.actions)
    assert (
        rendered.conclusions[0]
        .statements[-1]
        .endswith(f"Its planned actions are {actions}, not deployed changes.")
    )


@pytest.mark.parametrize("kind", sorted(DECLARED_SOURCE_KINDS), ids=str)
def test_every_declared_source_kind_qualifies_the_answer(kind: SnapshotSourceKind) -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    session.record.record_snapshot(session.index.snapshot_id, kind, session.index.collection_window)
    proposal = cited_proposal(
        session,
        [],
        status="insufficient_evidence",
        contexts=[{"side": "primary", "source_kind": kind.value}],
    )
    rendered = _render(proposal, session)
    assert rendered.snapshots[0].declared
    assert rendered.qualifications[0].startswith("The primary snapshot is ")
    assert "not" in rendered.qualifications[0] and "deployed state" in rendered.qualifications[0]


# ---- differences -------------------------------------------------------------------


def _difference(session: ToolSession) -> AnswerProposal:
    """A difference citing every resource on each side, as if both had been returned."""
    citations = []
    for side, index in (("primary", session.index), ("comparison", session.comparison_index)):
        session.record.record(index.snapshot_id, IdKind.RESOURCE, index.resource_ids)
        citations.append({"side": side, "resource_ids": list(index.resource_ids)})
    contexts = [
        {"side": "primary", "source_kind": session.index.source_kind.value},
        {"side": "comparison", "source_kind": session.comparison_index.source_kind.value},
    ]
    return cited_proposal(
        session,
        [],
        status="partial",
        contexts=contexts,
        conclusions=[{"kind": "recorded_difference", "citations": citations}],
    )


def test_differences_render_the_saved_comparison_entries() -> None:
    left, right = renamed_snapshots()
    comparison = compare_snapshots(left, right)
    session = ToolSession(left, comparison=right, comparisons=[comparison])
    rendered = _render(_difference(session), session)
    (conclusion,) = rendered.conclusions
    entries = [item for item in conclusion.statements if item.startswith("The saved comparison")]
    cited = len(comparison.matched) + len(comparison.unmatched) + len(comparison.ambiguous)
    assert len(entries) == min(cited, MAX_RENDERED_ENTRIES) > 0
    assert RECORDED_DIFFERENCES in rendered.qualifications
    (pair,) = comparison.matched
    (difference,) = pair.differences
    count = len(conclusion.data)
    assert _quoted(rendered)[-3:] == [difference.path, "null", '"renamed"']
    assert conclusion.statements[-1] == (
        f"Difference 1 (changed) at [{count - 2}]: the primary snapshot records [{count - 1}] "
        f"and the comparison snapshot records [{count}]. It is qualified by unrecorded_coverage."
    )
    assert [snapshot.side for snapshot in rendered.snapshots] == ["primary", "comparison"]


def test_a_snapshot_compared_with_itself_keeps_both_sides() -> None:
    snapshot = load_snapshot(OWNERSHIP_SNAPSHOT)
    session = ToolSession(
        snapshot, comparison=snapshot, comparisons=[compare_snapshots(snapshot, snapshot)]
    )
    (conclusion,) = _render(_difference(session), session).conclusions
    assert any(
        "on the primary snapshot with one on the comparison snapshot" in item
        for item in conclusion.statements
    )
    assert any(
        item.startswith("Resource 1 on the primary snapshot") for item in conclusion.statements
    )


def test_differences_without_a_saved_entry_say_so() -> None:
    left, right = renamed_snapshots()
    session = ToolSession(left, comparison=right)
    rendered = _render(_difference(session), session)
    assert NO_COMPARISON_ENTRY in rendered.conclusions[0].statements
    assert MISSING_DIFFERENCE in rendered.qualifications


def test_comparison_entries_and_differences_are_bounded(monkeypatch) -> None:
    left, right = renamed_snapshots()
    comparison = compare_snapshots(left, right)
    (pair,) = comparison.matched
    many = pair.model_copy(update={"differences": pair.differences * (MAX_RENDERED_ENTRIES + 3)})
    repeated = comparison.model_copy(update={"matched": [many] * (MAX_RENDERED_ENTRIES + 2)})
    session = ToolSession(left, comparison=right)
    # Too repetitive to pass comparison validation, which only the session applies.
    monkeypatch.setattr(ToolSession, "comparisons", property(lambda _: (repeated,)))
    (conclusion,) = _render(_difference(session), session).conclusions
    assert conclusion.statements[-2] == "2 more comparison entries are not shown."
    assert conclusion.statements[-1].endswith("more field differences are not shown.")
    shown = [item for item in conclusion.statements if item.startswith("Difference ")]
    assert len(shown) == MAX_RENDERED_ENTRIES


# ---- model and imported text -------------------------------------------------------


def test_fabricated_narrative_never_becomes_a_conclusion() -> None:
    session, delivered = analyzed_findings(AnalyzerKind.OWNERSHIP)
    proposal = cited_proposal(
        session,
        delivered,
        status=expected_status(delivered),
        summary=FABRICATED,
        explanation=f"{FABRICATED} Every other resource is also exposed.",
    )
    rendered = _render(proposal, session)
    assert rendered.narrative.summary == FABRICATED
    assert FABRICATED not in _wording(rendered)
    assert all(FABRICATED not in item for item in _quoted(rendered))
    assert "publicly reachable" not in rendered.model_dump_json(exclude={"narrative"})


def test_secrets_in_the_narrative_are_redacted() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    proposal = cited_proposal(
        session,
        [],
        status="insufficient_evidence",
        summary=f"Token {SECRET}.",
        explanation=f"Again {SECRET}.",
    )
    rendered = _render(proposal, session)
    assert SECRET not in rendered.model_dump_json()
    assert rendered.narrative == RenderedNarrative(
        summary=f"Token {REDACTED}.", explanation=f"Again {REDACTED}."
    )


@pytest.fixture
def injected(tmp_path: Path) -> ToolSession:
    """The network snapshot with an instruction as the name of every resource with evidence."""
    data = json.loads((SNAPSHOTS / "network.json").read_text(encoding="utf-8"))
    pending = list(data["regions"].values())
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("evidence_ids") and "identity" in item:
                item["name"] = INJECTION
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    path = tmp_path / "injected.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return ToolSession.from_paths(path, limits=ToolLimits(result_limit=100))


def test_injected_imported_text_appears_only_as_quoted_data(injected: ToolSession) -> None:
    resource = next(
        resource
        for resource in injected.index.resource_ids
        if injected.index.get_resource(resource).name == INJECTION
        and injected.index.resource_evidence_ids(resource)
    )
    proposal = cited_proposal(
        injected,
        [],
        status="insufficient_evidence",
        conclusions=[_configuration(injected, resource)],
    )
    rendered = _render(proposal, injected)
    assert INJECTION in _quoted(rendered)
    assert INJECTION not in _wording(rendered)
    assert INJECTION not in rendered.narrative.model_dump_json()


# ---- misuse ------------------------------------------------------------------------


def test_failed_runs_and_undelivered_findings_are_not_rendered_as_answers() -> None:
    session, delivered = analyzed_findings(AnalyzerKind.OWNERSHIP)
    proposal = cited_proposal(session, delivered, status=expected_status(delivered))
    with pytest.raises(ValueError, match="failure"):
        render_answer(proposal, session, AnswerOutcome(status="failed", failure="cancelled"))
    fresh = ToolSession(session.snapshot(session.index.snapshot_id))
    with pytest.raises(ValueError, match="not delivered"):
        render_answer(proposal, fresh, ANSWERED)


def test_rendering_does_not_change_the_run_record() -> None:
    session, delivered = analyzed_findings(AnalyzerKind.OWNERSHIP)
    before = session.record.context()
    render_answer(
        cited_proposal(session, delivered, status=expected_status(delivered)), session, ANSWERED
    )
    assert session.record.context() == before
