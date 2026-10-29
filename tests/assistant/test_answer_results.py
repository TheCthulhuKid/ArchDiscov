from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from archdiscov.assistant.answer_results import (
    FAILURE_EXIT_CODES,
    SUCCESS_EXIT_CODE,
    AnswerOutcome,
    FailureReason,
    RenderedAnswer,
    RenderedConclusion,
    RenderedFailure,
    RenderedNarrative,
    RenderedSnapshot,
    final_outcome,
    needs_repair,
)
from archdiscov.assistant.answers import DECLARED_SOURCE_KINDS, Citation
from archdiscov.assistant.runs import (
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    Proposed,
    RunLimitKind,
)
from archdiscov.assistant.tools.contracts import untrusted
from archdiscov.assistant.tools.views import AnalysisLimitationView, FindingView
from archdiscov.models.enums import AnalysisStatus, SnapshotSourceKind
from tests.support.run_outcomes import PROPOSAL, UNKNOWN_ID_REJECTIONS

# ---- the outcome contract ----------------------------------------------------------


def test_an_outcome_carries_exactly_the_detail_of_its_reason() -> None:
    limited = AnswerOutcome(status="failed", failure="limit_reached", limit="tool_calls")
    assert limited.limit == RunLimitKind.TOOL_CALLS
    for data in (
        {"status": "failed"},
        {"status": "failed", "failure": "limit_reached"},
        {"status": "answered", "rejections": UNKNOWN_ID_REJECTIONS},
    ):
        with pytest.raises(ValidationError):
            AnswerOutcome.model_validate(data)


def test_exit_codes() -> None:
    # 1 to 4 are the existing command codes; 130 is an interrupt, as for any command.
    assert FAILURE_EXIT_CODES == {
        FailureReason.REJECTED_AFTER_REPAIR: 5,
        FailureReason.NO_PROPOSAL_AFTER_REPAIR: 5,
        FailureReason.MALFORMED_OUTPUT: 5,
        FailureReason.PROVIDER_ERROR: 6,
        FailureReason.LIMIT_REACHED: 6,
        FailureReason.CANCELLED: 130,
    }
    assert AnswerOutcome(status="answered").exit_code == SUCCESS_EXIT_CODE
    assert SUCCESS_EXIT_CODE == 0
    assert AnswerOutcome(status="failed", failure="cancelled").exit_code == 130


# ---- mapping from runs -------------------------------------------------------------


def test_a_repairable_outcome_is_final_only_after_its_repair() -> None:
    # Repair is offered only for what a model can fix: a missing or rejected proposal.
    proposed = Proposed(proposal=PROPOSAL)
    invalid = MalformedOutput(reason=MalformedOutputReason.PROPOSAL_INVALID)
    limited = LimitReached(limit=RunLimitKind.TOOL_CALLS)
    assert needs_repair(proposed, UNKNOWN_ID_REJECTIONS) and needs_repair(invalid, ())
    assert not needs_repair(proposed, ()) and not needs_repair(limited, ())
    with pytest.raises(ValueError, match="only after its repair"):
        final_outcome(invalid, (), repaired=False)
    assert final_outcome(proposed, UNKNOWN_ID_REJECTIONS, repaired=True) == AnswerOutcome(
        status="failed", failure="rejected_after_repair", rejections=UNKNOWN_ID_REJECTIONS
    )
    assert final_outcome(limited, (), repaired=False) == AnswerOutcome(
        status="failed", failure="limit_reached", limit="tool_calls"
    )


def test_valid_proposals_answer_before_or_after_repair() -> None:
    proposed = Proposed(proposal=PROPOSAL)
    assert final_outcome(proposed, (), repaired=False) == AnswerOutcome(status="answered")
    assert final_outcome(proposed, (), repaired=True) == AnswerOutcome(
        status="answered_after_repair"
    )


# ---- the rendered answer -----------------------------------------------------------

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _snapshot(side: str = "primary", kind: SnapshotSourceKind = SnapshotSourceKind.AWS):
    return RenderedSnapshot(
        side=side,
        snapshot_id=UUID(int=1 if side == "primary" else 2),
        source_kind=kind,
        collection_started_at=NOW,
        collection_completed_at=NOW,
        declared=kind in DECLARED_SOURCE_KINDS,
    )


VIEW = FindingView(
    finding_id="f-1",
    status=AnalysisStatus.PARTIAL,
    summary=untrusted("Resource has no owner tag."),
    resources=(),
    evidence_ids=(),
    path=(),
    limitations=(AnalysisLimitationView(code="limited", message=untrusted("Limited.")),),
)
CONCLUSION = RenderedConclusion(
    kind="analyzer_finding",
    statements=("The analyzer reports this finding, quoted as [1].",),
    data=(VIEW.summary,),
    findings=(VIEW,),
    status=AnalysisStatus.PARTIAL,
    citations=(Citation(finding_ids=("f-1",)),),
)


def test_a_snapshot_is_declared_by_its_kind_and_ends_after_it_starts() -> None:
    snapshot = _snapshot(kind=SnapshotSourceKind.TERRAFORM_PLAN)
    assert snapshot.declared
    for update in ({"declared": False}, {"collection_completed_at": NOW - timedelta(seconds=1)}):
        with pytest.raises(ValidationError):
            RenderedSnapshot.model_validate({**snapshot.model_dump(), **update})


@pytest.mark.parametrize(
    "fields",
    [
        {
            "outcome": AnswerOutcome(status="failed", failure="cancelled"),
            "status": None,
            "conclusions": (),
            "failure": RenderedFailure(reason="cancelled", unverified=("Cancelled.",)),
        },
        {"conclusions": ()},
        {"snapshots": (_snapshot("comparison"), _snapshot())},
    ],
    ids=["failure-with-narrative", "supported-without-conclusions", "primary-not-first"],
)
def test_renderings_show_an_answer_or_a_failure(fields: dict) -> None:
    base = {
        "outcome": AnswerOutcome(status="answered"),
        "snapshots": (_snapshot(),),
        "status": AnalysisStatus.SUPPORTED,
        "conclusions": (CONCLUSION,),
        "narrative": RenderedNarrative(summary="s"),
    }
    with pytest.raises(ValidationError):
        RenderedAnswer.model_validate({**base, **fields})


@pytest.mark.parametrize(
    "fields",
    [{"status": AnalysisStatus.SUPPORTED}, {"statements": ("Quoted as [2].",)}],
    ids=["status-not-combined", "dangling"],
)
def test_conclusions_are_consistent_with_their_findings_and_data(fields: dict) -> None:
    with pytest.raises(ValidationError):
        RenderedConclusion.model_validate({**dict(CONCLUSION), **fields})
