"""How an answer run ended for the user, and the rendered answer built from its records.

`AnswerOutcome` turns a run's outcome (step 36) and its proposal's rejections (step 37) into
one of three stable statuses with an exit code. `RenderedAnswer` is what the readable and JSON
renderings are built from: facts written from records with fixed wording, never from the
model's narrative, which appears only in its labelled field.
"""

import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from archdiscov.models.enums import AnalysisStatus, SnapshotSourceKind
from archdiscov.models.validation_errors import validation_error

from .answers import (
    DECLARED_SOURCE_KINDS,
    MAX_CITED_IDS,
    AnswerSide,
    Citation,
    ConclusionKind,
    combined_status,
)
from .conversation import Contract
from .provider import ProviderErrorCode
from .rejections import Rejection
from .runs import (
    Answered,
    Cancelled,
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    Proposed,
    ProviderFailed,
    RunLimitKind,
    RunOutcome,
)
from .tools.contracts import UntrustedText
from .tools.views import FindingView


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    ANSWERED_AFTER_REPAIR = "answered_after_repair"
    FAILED = "failed"


class FailureReason(StrEnum):
    REJECTED_AFTER_REPAIR = "rejected_after_repair"
    NO_PROPOSAL_AFTER_REPAIR = "no_proposal_after_repair"
    MALFORMED_OUTPUT = "malformed_output"
    PROVIDER_ERROR = "provider_error"
    LIMIT_REACHED = "limit_reached"
    CANCELLED = "cancelled"


SUCCESS_EXIT_CODE = 0
FAILURE_EXIT_CODES: Mapping[FailureReason, int] = {
    # No answer could be validated.
    FailureReason.REJECTED_AFTER_REPAIR: 5,
    FailureReason.NO_PROPOSAL_AFTER_REPAIR: 5,
    FailureReason.MALFORMED_OUTPUT: 5,
    # The run was stopped; a script may retry.
    FailureReason.PROVIDER_ERROR: 6,
    FailureReason.LIMIT_REACHED: 6,
    # Interrupted, as for any command.
    FailureReason.CANCELLED: 130,
}

_PROPOSAL_REASONS = frozenset(
    {
        MalformedOutputReason.PROPOSAL_MALFORMED,
        MalformedOutputReason.PROPOSAL_INVALID,
        MalformedOutputReason.PROPOSAL_MIXED,
    }
)

# The detail field each failure reason carries; the others must be empty.
_DETAILS: Mapping[FailureReason, str | None] = {
    FailureReason.REJECTED_AFTER_REPAIR: "rejections",
    FailureReason.NO_PROPOSAL_AFTER_REPAIR: None,
    FailureReason.MALFORMED_OUTPUT: "malformed",
    FailureReason.PROVIDER_ERROR: "provider_error",
    FailureReason.LIMIT_REACHED: "limit",
    FailureReason.CANCELLED: None,
}


class AnswerOutcome(Contract):
    """The stable status of an answer run, with exactly the detail its failure needs."""

    status: AnswerStatus
    failure: FailureReason | None = None
    rejections: tuple[Rejection, ...] = ()
    """The final proposal's rejections, for `rejected_after_repair`."""
    malformed: MalformedOutputReason | None = None
    provider_error: ProviderErrorCode | None = None
    limit: RunLimitKind | None = None

    @model_validator(mode="after")
    def require_matching_detail(self) -> Self:
        if (self.status == AnswerStatus.FAILED) != (self.failure is not None):
            raise validation_error("answer_outcome_shape", "failed, and only it, has a reason")
        expected = None if self.failure is None else _DETAILS[self.failure]
        for name in set(_DETAILS.values()) - {None}:
            value = getattr(self, name)
            present = value != () if name == "rejections" else value is not None
            if present != (name == expected):
                raise validation_error("answer_outcome_shape", "detail must match the reason")
        return self

    @property
    def exit_code(self) -> int:
        return SUCCESS_EXIT_CODE if self.failure is None else FAILURE_EXIT_CODES[self.failure]


def needs_repair(outcome: RunOutcome, rejections: tuple[Rejection, ...]) -> bool:
    """Whether one repair request could fix how the run ended."""
    if isinstance(outcome, Proposed):
        return bool(rejections)
    if isinstance(outcome, MalformedOutput):
        return outcome.reason in _PROPOSAL_REASONS
    return isinstance(outcome, Answered)


def final_outcome(
    outcome: RunOutcome, rejections: tuple[Rejection, ...], *, repaired: bool
) -> AnswerOutcome:
    """The answer outcome for the last run segment; `repaired` says it was the repair.

    An outcome that still needs repair is final only after the repair was made.
    """
    if needs_repair(outcome, rejections) and not repaired:
        raise ValueError("a repairable outcome is final only after its repair")
    if not isinstance(outcome, Proposed):
        return _FAILURES[type(outcome)](outcome)
    if rejections:
        return _failed(FailureReason.REJECTED_AFTER_REPAIR, rejections=rejections)
    status = AnswerStatus.ANSWERED_AFTER_REPAIR if repaired else AnswerStatus.ANSWERED
    return AnswerOutcome(status=status)


def _failed(reason: FailureReason, **detail) -> AnswerOutcome:
    return AnswerOutcome(status=AnswerStatus.FAILED, failure=reason, **detail)


# Every run outcome other than a proposal ends the answer with a failure.
_FAILURES: Mapping[type, Callable[[RunOutcome], AnswerOutcome]] = {
    Answered: lambda _: _failed(FailureReason.NO_PROPOSAL_AFTER_REPAIR),
    MalformedOutput: lambda outcome: _failed(
        FailureReason.MALFORMED_OUTPUT, malformed=outcome.reason
    ),
    ProviderFailed: lambda outcome: _failed(
        FailureReason.PROVIDER_ERROR, provider_error=outcome.code
    ),
    LimitReached: lambda outcome: _failed(FailureReason.LIMIT_REACHED, limit=outcome.limit),
    Cancelled: lambda _: _failed(FailureReason.CANCELLED),
}


# ---- the rendered answer -----------------------------------------------------------

Statement = Annotated[str, Field(min_length=1, max_length=500)]

MAX_RENDERED_ENTRIES = 20
# A statement per cited ID on each of up to two sides, the comparison entries and differences,
# and counts of those left out; a statement quotes at most three values.
MAX_STATEMENTS = 2 * 3 * MAX_CITED_IDS + 2 * MAX_RENDERED_ENTRIES + 2
MAX_QUOTED = 3 * MAX_STATEMENTS
_REFERENCE = re.compile(r"\[(\d+)\]")


class RenderedSnapshot(Contract):
    """The snapshot behind one side of the answer."""

    side: AnswerSide
    snapshot_id: UUID
    source_kind: SnapshotSourceKind
    collection_started_at: AwareDatetime
    collection_completed_at: AwareDatetime
    declared: bool

    @model_validator(mode="after")
    def require_consistent_snapshot(self) -> Self:
        if self.declared != (self.source_kind in DECLARED_SOURCE_KINDS):
            raise validation_error("answer_rendering_shape", "declared must match source kind")
        if self.collection_completed_at < self.collection_started_at:
            raise validation_error("answer_rendering_shape", "collection must not end first")
        return self


class RenderedConclusion(Contract):
    """One conclusion written from records: fixed statements, quoted data, and citations.

    Cited findings appear as the views delivered to the model, limitations included.
    """

    kind: ConclusionKind
    statements: tuple[Statement, ...] = Field(min_length=1, max_length=MAX_STATEMENTS)
    data: tuple[UntrustedText, ...] = Field(default=(), max_length=MAX_QUOTED)
    findings: tuple[FindingView, ...] = Field(default=(), max_length=2 * MAX_CITED_IDS)
    status: AnalysisStatus | None = None
    citations: tuple[Citation, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def require_consistent_conclusion(self) -> Self:
        if self.kind == ConclusionKind.ANALYZER_FINDING and not self.findings:
            raise validation_error("answer_rendering_shape", "an analyzer conclusion has findings")
        expected = combined_status(view.status for view in self.findings) if self.findings else None
        if self.status != expected:
            raise validation_error("answer_rendering_shape", "status combines the findings")
        for statement in self.statements:
            for reference in _REFERENCE.findall(statement):
                if not 1 <= int(reference) <= len(self.data):
                    raise validation_error("answer_rendering_shape", "references quoted data")
        return self


class RenderedNarrative(Contract):
    """The model's own words, shown only as labelled explanation, never as facts."""

    summary: Annotated[str, Field(min_length=1, max_length=500)]
    explanation: Annotated[str, Field(min_length=1, max_length=2_000)] | None = None


class RenderedFailure(Contract):
    """What a failed run retrieved and what it could not verify; no provider text."""

    reason: FailureReason
    retrieved: tuple[RenderedConclusion, ...] = Field(default=(), max_length=100)
    unverified: tuple[Statement, ...] = Field(min_length=1, max_length=50)


class RenderedAnswer(Contract):
    """Everything a rendering shows, for an answer or a failure."""

    outcome: AnswerOutcome
    snapshots: tuple[RenderedSnapshot, ...] = Field(min_length=1, max_length=2)
    status: AnalysisStatus | None = None
    conclusions: tuple[RenderedConclusion, ...] = Field(default=(), max_length=20)
    qualifications: tuple[Statement, ...] = Field(default=(), max_length=20)
    narrative: RenderedNarrative | None = None
    failure: RenderedFailure | None = None

    @model_validator(mode="after")
    def require_answer_or_failure(self) -> Self:
        failed = self.outcome.status == AnswerStatus.FAILED
        if failed:
            # A failure never shows the rejected proposal's narrative or conclusions.
            if self.failure is None or self.failure.reason != self.outcome.failure:
                raise validation_error("answer_rendering_shape", "a failure needs its reason")
            if self.status is not None or self.conclusions or self.narrative is not None:
                raise validation_error("answer_rendering_shape", "a failure shows no answer")
        elif self.failure is not None or self.status is None or self.narrative is None:
            raise validation_error("answer_rendering_shape", "an answer needs status and narrative")
        elif self.status != AnalysisStatus.INSUFFICIENT_EVIDENCE and not self.conclusions:
            # As in the proposal: an affirmative status always shows what supports it.
            raise validation_error(
                "answer_rendering_shape", "supported and partial need conclusions"
            )
        sides = [snapshot.side for snapshot in self.snapshots]
        if sides[0] != AnswerSide.PRIMARY or len(set(sides)) != len(sides):
            raise validation_error("answer_rendering_shape", "one snapshot per side, primary first")
        return self
