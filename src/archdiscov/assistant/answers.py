"""The structured answer a provider proposes, and the `submit_answer` tool that carries it.

A proposal says which recorded items support which kind of claim; it never states the facts
itself. The renderer (step 38) writes factual conclusions from validated findings and
evidence, and shows the narrative fields only as labelled explanation. Validation against the
run (step 37.4–37.5) checks citations and consistency, not truth.

Citations name a side, `primary` or `comparison`, rather than a snapshot: the session fixes
which snapshot each side is, so the model never handles snapshot IDs.
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from archdiscov.models.enums import AnalysisStatus, SnapshotSourceKind
from archdiscov.models.resource_base import NonEmptyString
from archdiscov.models.validation_errors import validation_error

from .conversation import Contract, ToolDeclaration
from .tools.inputs import ToolInput, inline_schema

SUBMIT_ANSWER = "submit_answer"
MAX_CONCLUSIONS = 20
MAX_CITED_IDS = 50
MAX_LIMITATIONS = 100
MAX_SUMMARY_CHARS = 500
MAX_EXPLANATION_CHARS = 2_000

CitedId = Annotated[NonEmptyString, Field(max_length=2_048)]
CitedIds = Annotated[tuple[CitedId, ...], Field(default=(), max_length=MAX_CITED_IDS)]
LimitationCode = Annotated[str, Field(max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]


# Snapshots that record declared configuration, not observed, deployed state.
DECLARED_SOURCE_KINDS = frozenset(
    {
        SnapshotSourceKind.TERRAFORM_STATE,
        SnapshotSourceKind.TERRAFORM_PLAN,
        SnapshotSourceKind.CLOUDFORMATION,
    }
)


def combined_status(statuses: Iterable[AnalysisStatus]) -> AnalysisStatus:
    """What findings support together: supported or insufficient only if all are, else partial."""
    distinct = set(statuses)
    if not distinct:
        raise ValueError("a combined status needs at least one finding")
    return distinct.pop() if len(distinct) == 1 else AnalysisStatus.PARTIAL


class AnswerSide(StrEnum):
    PRIMARY = "primary"
    COMPARISON = "comparison"


class ConclusionKind(StrEnum):
    ANALYZER_FINDING = "analyzer_finding"
    RECORDED_CONFIGURATION = "recorded_configuration"
    RECORDED_DIFFERENCE = "recorded_difference"


class InsufficientReason(StrEnum):
    NO_APPLICABLE_FINDINGS = "no_applicable_findings"
    LIMITED_COVERAGE = "limited_coverage"
    UNSUPPORTED_ANALYSIS = "unsupported_analysis"
    OUT_OF_SCOPE = "out_of_scope"


def _unique(values: tuple, what: str) -> None:
    if len(set(values)) != len(values):
        raise validation_error("answer_duplicate", f"duplicate {what}")


class Citation(Contract):
    """Recorded items on one side that a conclusion rests on."""

    side: AnswerSide = AnswerSide.PRIMARY
    finding_ids: CitedIds
    evidence_ids: CitedIds = Field(
        default=(),
        max_length=MAX_CITED_IDS,
        description="Each must belong to a finding or a resource cited in this citation.",
    )
    resource_ids: CitedIds

    @model_validator(mode="after")
    def validate_ids(self) -> Self:
        if not (self.finding_ids or self.evidence_ids or self.resource_ids):
            raise validation_error("answer_citation_empty", "a citation names at least one ID")
        for ids, what in (
            (self.finding_ids, "finding IDs"),
            (self.evidence_ids, "evidence IDs"),
            (self.resource_ids, "resource IDs"),
        ):
            _unique(ids, what)
        return self


class Conclusion(Contract):
    """One kind of claim and the citations it rests on; the claim's wording is rendered."""

    kind: ConclusionKind
    citations: tuple[Citation, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        sides = [citation.side for citation in self.citations]
        _unique(tuple(sides), "citation sides")
        if self.kind == ConclusionKind.RECORDED_DIFFERENCE:
            if set(sides) != set(AnswerSide):
                raise validation_error("answer_conclusion_shape", "a difference cites both sides")
        elif len(sides) != 1:
            raise validation_error("answer_conclusion_shape", "cites one side")
        if self.kind == ConclusionKind.ANALYZER_FINDING and not self.citations[0].finding_ids:
            raise validation_error("answer_conclusion_shape", "rests on finding IDs")
        if (
            self.kind == ConclusionKind.RECORDED_CONFIGURATION
            and not self.citations[0].evidence_ids
        ):
            raise validation_error("answer_conclusion_shape", "rests on evidence IDs")
        return self


class AcknowledgedLimitation(Contract):
    """A limitation of a cited finding that the answer accepts and will state."""

    side: AnswerSide = AnswerSide.PRIMARY
    finding_id: CitedId
    limitation: LimitationCode


class AnswerContext(Contract):
    """What a side's snapshot records, so planned or declared state is never shown as deployed."""

    side: AnswerSide
    source_kind: SnapshotSourceKind


class AnswerProposal(ToolInput):
    """The provider's structured answer: status, cited conclusions, and bounded narrative."""

    status: AnalysisStatus = Field(
        description="supported only if every cited finding is supported and its limitations "
        "are acknowledged; insufficient_evidence when nothing applicable was found."
    )
    contexts: tuple[AnswerContext, ...] = Field(
        min_length=1,
        max_length=2,
        description="The source kind of each side's snapshot, exactly as tools reported it.",
    )
    conclusions: tuple[Conclusion, ...] = Field(
        default=(),
        max_length=MAX_CONCLUSIONS,
        description="Each claim's kind and the returned IDs it rests on; the wording of the "
        "facts is produced from these records.",
    )
    limitations: tuple[AcknowledgedLimitation, ...] = Field(
        default=(),
        max_length=MAX_LIMITATIONS,
        description="Every limitation code of every cited finding.",
    )
    insufficient_reason: InsufficientReason | None = Field(
        default=None, description="Required for insufficient_evidence, and only then."
    )
    summary: Annotated[NonEmptyString, Field(max_length=MAX_SUMMARY_CHARS)] = Field(
        description="A short explanation shown as such; never a source of facts."
    )
    explanation: Annotated[NonEmptyString, Field(max_length=MAX_EXPLANATION_CHARS)] | None = Field(
        default=None, description="Optional longer explanation; never a source of facts."
    )

    @model_validator(mode="after")
    def validate_proposal(self) -> Self:
        sides = tuple(context.side for context in self.contexts)
        _unique(sides, "context sides")
        if AnswerSide.PRIMARY not in sides:
            raise validation_error("answer_context_missing", "the primary side needs a context")
        cited = {
            citation.side for conclusion in self.conclusions for citation in conclusion.citations
        }
        cited |= {limitation.side for limitation in self.limitations}
        if not cited <= set(sides):
            raise validation_error("answer_context_missing", "every cited side needs a context")
        _unique(self.conclusions, "conclusions")
        _unique(self.limitations, "acknowledged limitations")
        insufficient = self.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
        if insufficient != (self.insufficient_reason is not None):
            raise validation_error(
                "answer_status_shape", "insufficient_evidence, and only it, needs a reason"
            )
        if not insufficient and not self.conclusions:
            raise validation_error("answer_status_shape", "supported and partial need conclusions")
        return self


def submit_answer_declaration() -> ToolDeclaration:
    """The terminal tool whose input is the proposal; it is never dispatched as a tool."""
    return ToolDeclaration(
        name=SUBMIT_ANSWER,
        description=(
            "Submit the final answer exactly once, after using the other tools. Cite only "
            "finding, evidence, and resource IDs that tools returned in this conversation, "
            "cite each piece of evidence with the finding or resource it belongs to, "
            "acknowledge every limitation of the findings you cite, and keep the summary and "
            "explanation to explanation: facts are taken from the cited records."
        ),
        input_schema=inline_schema(AnswerProposal.model_json_schema()),
    )
