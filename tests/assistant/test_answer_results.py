from datetime import UTC, datetime
from typing import get_args
from uuid import UUID

import pytest
from pydantic import ValidationError

from archdiscov.assistant.answer_results import (
    FAILURE_EXIT_CODES,
    SUCCESS_EXIT_CODE,
    AnswerOutcome,
    AnswerStatus,
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
    Answered,
    Cancelled,
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    Proposed,
    ProviderFailed,
    RunOutcome,
)
from archdiscov.assistant.tools.contracts import untrusted
from archdiscov.assistant.tools.views import AnalysisLimitationView, FindingView
from archdiscov.models.enums import AnalysisStatus, SnapshotSourceKind
from tests.support.run_outcomes import PROPOSAL, UNKNOWN_ID_REJECTIONS, every_run_outcome

PROPOSAL_REASONS = {
    MalformedOutputReason.PROPOSAL_MALFORMED,
    MalformedOutputReason.PROPOSAL_INVALID,
    MalformedOutputReason.PROPOSAL_MIXED,
}


def test_examples_cover_every_run_outcome_kind() -> None:
    members = get_args(get_args(RunOutcome)[0])
    assert {type(item) for item in every_run_outcome()} == set(members)


# ---- the outcome contract ----------------------------------------------------------

VALID = [
    {"status": "answered"},
    {"status": "answered_after_repair"},
    {
        "status": "failed",
        "failure": "rejected_after_repair",
        "rejections": [item.model_dump(mode="json") for item in UNKNOWN_ID_REJECTIONS],
    },
    {"status": "failed", "failure": "no_proposal_after_repair"},
    {"status": "failed", "failure": "malformed_output", "malformed": "provider_reported"},
    {"status": "failed", "failure": "provider_error", "provider_error": "provider_timeout"},
    {"status": "failed", "failure": "limit_reached", "limit": "overall_deadline"},
    {"status": "failed", "failure": "cancelled"},
]


@pytest.mark.parametrize("data", VALID, ids=lambda data: data.get("failure", data["status"]))
def test_every_status_and_reason_round_trips(data: dict) -> None:
    outcome = AnswerOutcome.model_validate(data)
    assert AnswerOutcome.model_validate_json(outcome.model_dump_json()) == outcome


def test_examples_cover_every_failure_reason() -> None:
    assert {item.get("failure") for item in VALID} - {None} == {
        reason.value for reason in FailureReason
    }


@pytest.mark.parametrize(
    "data",
    [
        {"status": "failed"},
        {"status": "answered", "failure": "cancelled"},
        {"status": "failed", "failure": "limit_reached"},
        {"status": "failed", "failure": "cancelled", "limit": "tool_calls"},
        {"status": "failed", "failure": "provider_error", "limit": "tool_calls"},
        {"status": "answered", "malformed": "proposal_invalid"},
        {"status": "failed", "failure": "rejected_after_repair"},
        {"status": "failed", "failure": "gave_up"},
        {"status": "answered", "confidence": 1},
    ],
)
def test_inconsistent_outcomes_are_rejected(data: dict) -> None:
    with pytest.raises(ValidationError):
        AnswerOutcome.model_validate(data)


# ---- exit codes --------------------------------------------------------------------


def test_exit_codes() -> None:
    assert set(FAILURE_EXIT_CODES) == set(FailureReason)
    assert set(FAILURE_EXIT_CODES.values()) == {5, 6, 130}
    assert {reason for reason, code in FAILURE_EXIT_CODES.items() if code == 5} == {
        FailureReason.REJECTED_AFTER_REPAIR,
        FailureReason.NO_PROPOSAL_AFTER_REPAIR,
        FailureReason.MALFORMED_OUTPUT,
    }
    assert {reason for reason, code in FAILURE_EXIT_CODES.items() if code == 6} == {
        FailureReason.PROVIDER_ERROR,
        FailureReason.LIMIT_REACHED,
    }
    assert FAILURE_EXIT_CODES[FailureReason.CANCELLED] == 130
    assert SUCCESS_EXIT_CODE == 0
    for data in VALID:
        outcome = AnswerOutcome.model_validate(data)
        expected = 0 if outcome.failure is None else FAILURE_EXIT_CODES[outcome.failure]
        assert outcome.exit_code == expected


def test_exit_codes_avoid_the_existing_cli_codes() -> None:
    assert not set(FAILURE_EXIT_CODES.values()) & {1, 2, 3, 4}


# ---- mapping from runs -------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    every_run_outcome(),
    ids=lambda item: f"{item.kind}-{getattr(item, 'reason', getattr(item, 'limit', ''))}",
)
def test_repair_is_offered_only_for_what_a_model_can_fix(outcome: RunOutcome) -> None:
    repairable = isinstance(outcome, Answered) or (
        isinstance(outcome, MalformedOutput) and outcome.reason in PROPOSAL_REASONS
    )
    if isinstance(outcome, Proposed):
        assert needs_repair(outcome, UNKNOWN_ID_REJECTIONS) is True
        assert needs_repair(outcome, ()) is False
    else:
        assert needs_repair(outcome, ()) is repairable
        assert needs_repair(outcome, UNKNOWN_ID_REJECTIONS) is repairable


@pytest.mark.parametrize(
    "outcome",
    every_run_outcome(),
    ids=lambda item: f"{item.kind}-{getattr(item, 'reason', getattr(item, 'limit', ''))}",
)
def test_every_run_outcome_maps_to_one_answer_outcome(outcome: RunOutcome) -> None:
    rejections = UNKNOWN_ID_REJECTIONS if isinstance(outcome, Proposed) else ()
    if needs_repair(outcome, rejections):
        with pytest.raises(ValueError, match="only after its repair"):
            final_outcome(outcome, rejections, repaired=False)
    mapped = final_outcome(outcome, rejections, repaired=True)
    expected = {
        Proposed: (FailureReason.REJECTED_AFTER_REPAIR, "rejections", UNKNOWN_ID_REJECTIONS),
        Answered: (FailureReason.NO_PROPOSAL_AFTER_REPAIR, None, None),
        MalformedOutput: (
            FailureReason.MALFORMED_OUTPUT,
            "malformed",
            getattr(outcome, "reason", None),
        ),
        ProviderFailed: (
            FailureReason.PROVIDER_ERROR,
            "provider_error",
            getattr(outcome, "code", None),
        ),
        LimitReached: (FailureReason.LIMIT_REACHED, "limit", getattr(outcome, "limit", None)),
        Cancelled: (FailureReason.CANCELLED, None, None),
    }[type(outcome)]
    reason, field, value = expected
    assert mapped.status == AnswerStatus.FAILED
    assert mapped.failure == reason
    if field is not None:
        assert getattr(mapped, field) == value


def test_valid_proposals_answer_before_or_after_repair() -> None:
    proposed = Proposed(proposal=PROPOSAL)
    assert final_outcome(proposed, (), repaired=False) == AnswerOutcome(status="answered")
    assert final_outcome(proposed, (), repaired=True) == AnswerOutcome(
        status="answered_after_repair"
    )
    assert final_outcome(proposed, (), repaired=False).exit_code == 0


def test_unrepairable_outcomes_fail_without_repair() -> None:
    for outcome in every_run_outcome():
        if not needs_repair(outcome, ()) and not isinstance(outcome, Proposed):
            assert final_outcome(outcome, (), repaired=False).status == AnswerStatus.FAILED


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


@pytest.mark.parametrize("kind", list(SnapshotSourceKind), ids=lambda kind: kind.value)
def test_snapshots_flag_declared_state(kind: SnapshotSourceKind) -> None:
    snapshot = _snapshot(kind=kind)
    assert snapshot.declared == (kind in DECLARED_SOURCE_KINDS)
    with pytest.raises(ValidationError):
        snapshot.model_validate({**snapshot.model_dump(), "declared": not snapshot.declared})


def test_answers_and_failures_round_trip() -> None:
    answer = RenderedAnswer(
        outcome=AnswerOutcome(status="answered"),
        snapshots=(_snapshot(), _snapshot("comparison", SnapshotSourceKind.TERRAFORM_PLAN)),
        status=AnalysisStatus.PARTIAL,
        conclusions=(CONCLUSION,),
        qualifications=("Coverage of one operation failed.",),
        narrative=RenderedNarrative(summary="One resource lacks an owner."),
    )
    failure = RenderedAnswer(
        outcome=AnswerOutcome(status="failed", failure="limit_reached", limit="tool_calls"),
        snapshots=(_snapshot(),),
        failure=RenderedFailure(
            reason="limit_reached",
            retrieved=(CONCLUSION,),
            unverified=("The run reached its tool-call limit.",),
        ),
    )
    for rendered in (answer, failure):
        assert RenderedAnswer.model_validate_json(rendered.model_dump_json()) == rendered


@pytest.mark.parametrize(
    "fields",
    [
        {
            "outcome": AnswerOutcome(status="answered"),
            "narrative": None,
            "status": AnalysisStatus.SUPPORTED,
        },
        {"outcome": AnswerOutcome(status="answered"), "status": None},
        {
            "outcome": AnswerOutcome(status="failed", failure="cancelled"),
            "status": None,
            "narrative": None,
        },
        {
            "outcome": AnswerOutcome(status="failed", failure="cancelled"),
            "status": None,
            "failure": RenderedFailure(reason="cancelled", unverified=("Cancelled.",)),
        },
        {
            "outcome": AnswerOutcome(status="failed", failure="cancelled"),
            "status": None,
            "narrative": None,
            "failure": RenderedFailure(reason="limit_reached", unverified=("Stopped.",)),
        },
        {
            "outcome": AnswerOutcome(status="failed", failure="cancelled"),
            "status": None,
            "narrative": None,
            "conclusions": (CONCLUSION,),
            "failure": RenderedFailure(reason="cancelled", unverified=("Cancelled.",)),
        },
        {"snapshots": (_snapshot("comparison"),)},
        {"snapshots": (_snapshot("comparison"), _snapshot())},
        {"snapshots": (_snapshot(), _snapshot())},
    ],
    ids=[
        "answer-without-narrative",
        "answer-without-status",
        "failure-without-explanation",
        "failure-with-narrative",
        "failure-with-other-reason",
        "failure-with-conclusions",
        "no-primary",
        "primary-not-first",
        "duplicate-side",
    ],
)
def test_renderings_show_an_answer_or_a_failure(fields: dict) -> None:
    base = {
        "outcome": AnswerOutcome(status="answered"),
        "snapshots": (_snapshot(),),
        "status": AnalysisStatus.SUPPORTED,
        "narrative": RenderedNarrative(summary="s"),
    }
    with pytest.raises(ValidationError):
        RenderedAnswer.model_validate({**base, **fields})


def test_rendered_bounds() -> None:
    with pytest.raises(ValidationError):
        RenderedConclusion.model_validate({**CONCLUSION.model_dump(), "statements": ()})
    with pytest.raises(ValidationError):
        RenderedConclusion.model_validate({**CONCLUSION.model_dump(), "statements": ("x" * 501,)})
    with pytest.raises(ValidationError):
        RenderedFailure(reason="cancelled", unverified=())
    with pytest.raises(ValidationError):
        RenderedNarrative(summary="s", explanation="e" * 2_001)


def test_affirmative_answers_must_show_their_conclusions() -> None:
    base = {
        "outcome": AnswerOutcome(status="answered"),
        "snapshots": (_snapshot(),),
        "narrative": RenderedNarrative(summary="s"),
    }
    for status in (AnalysisStatus.SUPPORTED, AnalysisStatus.PARTIAL):
        with pytest.raises(ValidationError):
            RenderedAnswer.model_validate({**base, "status": status})
        assert RenderedAnswer.model_validate(
            {**base, "status": status, "conclusions": (CONCLUSION,)}
        )
    insufficient = RenderedAnswer.model_validate(
        {**base, "status": AnalysisStatus.INSUFFICIENT_EVIDENCE}
    )
    assert insufficient.conclusions == ()


def test_a_collection_window_cannot_end_before_it_starts() -> None:
    from datetime import timedelta

    with pytest.raises(ValidationError):
        RenderedSnapshot(
            side="primary",
            snapshot_id=UUID(int=1),
            source_kind=SnapshotSourceKind.AWS,
            collection_started_at=NOW,
            collection_completed_at=NOW - timedelta(seconds=1),
            declared=False,
        )


def test_details_are_checked_by_presence_not_truthiness() -> None:
    with pytest.raises(ValidationError):
        AnswerOutcome(status="failed", failure="rejected_after_repair", rejections=())
    with pytest.raises(ValidationError):
        AnswerOutcome(status="answered", rejections=UNKNOWN_ID_REJECTIONS)


@pytest.mark.parametrize(
    "fields",
    [
        {"findings": (), "status": None},
        {"status": AnalysisStatus.SUPPORTED},
        {"status": None},
        {"statements": ("Quoted as [2].",)},
        {"statements": ("Quoted as [0].",)},
    ],
    ids=["analyzer-without-findings", "status-not-combined", "no-status", "dangling", "zero"],
)
def test_conclusions_are_consistent_with_their_findings_and_data(fields: dict) -> None:
    with pytest.raises(ValidationError):
        RenderedConclusion.model_validate({**dict(CONCLUSION), **fields})


def test_conclusions_without_findings_have_no_status() -> None:
    configuration = {
        **dict(CONCLUSION),
        "kind": "recorded_configuration",
        "findings": (),
        "citations": (Citation(evidence_ids=("e-1",)),),
    }
    assert RenderedConclusion.model_validate({**configuration, "status": None}).status is None
    with pytest.raises(ValidationError):
        RenderedConclusion.model_validate({**configuration, "status": AnalysisStatus.PARTIAL})
