"""Validation of an answer proposal against the run that produced it.

Validation checks citations and consistency, not truth: a proposal that passes cites only
records the run actually delivered, for the snapshots the user selected. Every failure is a
`Rejection` with a stable code, the location within the proposal, and a fixed message that
never repeats proposal or tool text, so one repair request can address them all.
"""

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from archdiscov.graph import GraphIndex
from archdiscov.models.validation_errors import validation_error
from archdiscov.services.analysis_validation import (
    AnalysisValidationError,
    validate_analysis_result,
)

from .answers import AnswerProposal, AnswerSide, Citation
from .conversation import Contract
from .tools.contracts import IdKind
from .tools.record import RunContext
from .tools.session import ToolSession


class RejectionCode(StrEnum):
    SIDE_UNAVAILABLE = "answer_side_unavailable"
    SOURCE_KIND_MISMATCH = "answer_source_kind_mismatch"
    UNKNOWN_ID = "answer_unknown_id"
    OTHER_SNAPSHOT_ID = "answer_other_snapshot_id"
    NOT_RETURNED = "answer_id_not_returned"
    EVIDENCE_DETACHED = "answer_evidence_detached"
    RESULT_ALTERED = "answer_result_altered"


REJECTION_MESSAGES: Mapping[RejectionCode, str] = {
    RejectionCode.SIDE_UNAVAILABLE: "No comparison snapshot was selected for this session.",
    RejectionCode.SOURCE_KIND_MISMATCH: (
        "The source kind differs from the one the tools reported for this snapshot."
    ),
    RejectionCode.UNKNOWN_ID: "This ID is not recorded in the snapshot for this side.",
    RejectionCode.OTHER_SNAPSHOT_ID: (
        "This ID belongs to the other selected snapshot; cite it on that side."
    ),
    RejectionCode.NOT_RETURNED: "No tool returned this ID in this conversation.",
    RejectionCode.EVIDENCE_DETACHED: (
        "This evidence belongs neither to a finding in the same citation nor to a resource "
        "a tool returned."
    ),
    RejectionCode.RESULT_ALTERED: (
        "The analysis result behind this finding no longer passes its integrity checks."
    ),
}

ProposalLocation = Annotated[str, Field(pattern=r"^[a-z_]+(?:\[\d+\]|\.[a-z_]+)*$")]


class Rejection(Contract):
    """One reason a proposal fails; the message is fixed by the code."""

    code: RejectionCode
    location: ProposalLocation
    message: str

    @model_validator(mode="after")
    def require_fixed_message(self) -> Self:
        if self.message != REJECTION_MESSAGES[self.code]:
            raise validation_error("answer_rejection_message", "message is fixed by code")
        return self


def _reject(code: RejectionCode, location: str) -> Rejection:
    return Rejection(code=code, location=location, message=REJECTION_MESSAGES[code])


class _Checker:
    """Classifies cited IDs and re-verifies cited results for one session."""

    def __init__(self, session: ToolSession) -> None:
        self.session = session
        self.context: RunContext = session.record.context()
        self.indexes = {AnswerSide.PRIMARY: session.index}
        if session.comparison_index is not None:
            self.indexes[AnswerSide.COMPARISON] = session.comparison_index
        self.saved_findings = {
            (result.snapshot_id, finding.id)
            for result in session.analyses
            for finding in result.findings
        }
        self._intact: dict[str, bool] = {}

    def _other(self, side: AnswerSide) -> GraphIndex | None:
        return next((index for key, index in self.indexes.items() if key != side), None)

    def _exists(self, kind: IdKind, value: str, index: GraphIndex) -> bool:
        if kind == IdKind.RESOURCE:
            return index.get_resource(value) is not None
        if kind == IdKind.EVIDENCE:
            return index.get_evidence(value) is not None
        key = (index.snapshot_id, value)
        return self.context.finding(*key) is not None or key in self.saved_findings

    def id_rejection(self, side: AnswerSide, kind: IdKind, value: str) -> RejectionCode | None:
        """Why a cited ID cannot be used on this side, or None when it can."""
        index = self.indexes[side]
        if self.context.returned.contains(kind, index.snapshot_id, value):
            return None
        if self._exists(kind, value, index):
            return RejectionCode.NOT_RETURNED
        other = self._other(side)
        if other is not None and self._exists(kind, value, other):
            return RejectionCode.OTHER_SNAPSHOT_ID
        return RejectionCode.UNKNOWN_ID

    def detached(self, side: AnswerSide, citation: Citation, evidence_id: str) -> bool:
        """Returned evidence that neither a cited finding nor a returned resource owns."""
        index = self.indexes[side]
        attached = {
            evidence
            for finding_id in citation.finding_ids
            if (finding := self.context.finding(index.snapshot_id, finding_id)) is not None
            for evidence in finding.evidence_ids
        }
        if evidence_id in attached:
            return False
        owner = index.get_evidence(evidence_id).resource_id
        return not self.context.returned.contains(IdKind.RESOURCE, index.snapshot_id, owner)

    def altered(self, side: AnswerSide, finding_id: str) -> bool:
        """Whether any result the finding was delivered from fails re-verification."""
        finding = self.context.finding(self.indexes[side].snapshot_id, finding_id)
        return finding is not None and not all(
            self._result_intact(key) for key in finding.result_keys
        )

    def _result_intact(self, key: str) -> bool:
        if key not in self._intact:
            self._intact[key] = self._verify(key)
        return self._intact[key]

    def _verify(self, key: str) -> bool:
        result = dict(self.context.results).get(key)
        snapshot = None if result is None else self.session.snapshot(result.snapshot_id)
        if snapshot is None:
            return False
        try:
            checked = validate_analysis_result(result, snapshot)
        except AnalysisValidationError:
            return False
        if not key.startswith("saved:"):
            return True
        number = int(key.removeprefix("saved:"))
        analyses = self.session.analyses
        return 0 < number <= len(analyses) and checked == analyses[number - 1]


_CITED = (
    ("finding_ids", IdKind.FINDING),
    ("evidence_ids", IdKind.EVIDENCE),
    ("resource_ids", IdKind.RESOURCE),
)


def check_citations(proposal: AnswerProposal, session: ToolSession) -> tuple[Rejection, ...]:
    """Every citation failure in the proposal, ordered by location."""
    checker = _Checker(session)
    rejections = [
        *_check_contexts(proposal, checker),
        *_check_conclusions(proposal, checker),
        *_check_limitations(proposal, checker),
    ]
    return tuple(sorted(set(rejections), key=lambda item: (item.location, item.code)))


def _check_contexts(proposal: AnswerProposal, checker: _Checker) -> Iterator[Rejection]:
    for number, item in enumerate(proposal.contexts):
        index = checker.indexes.get(item.side)
        if index is None:
            yield _reject(RejectionCode.SIDE_UNAVAILABLE, f"contexts[{number}].side")
        elif checker.context.snapshot(index.snapshot_id).source_kind != item.source_kind:
            yield _reject(RejectionCode.SOURCE_KIND_MISMATCH, f"contexts[{number}].source_kind")


def _check_conclusions(proposal: AnswerProposal, checker: _Checker) -> Iterator[Rejection]:
    for number, conclusion in enumerate(proposal.conclusions):
        for position, citation in enumerate(conclusion.citations):
            location = f"conclusions[{number}].citations[{position}]"
            if citation.side not in checker.indexes:
                yield _reject(RejectionCode.SIDE_UNAVAILABLE, f"{location}.side")
            else:
                yield from _check_citation(citation, checker, location)


def _check_citation(citation: Citation, checker: _Checker, location: str) -> Iterator[Rejection]:
    side = citation.side
    for field, kind in _CITED:
        for position, value in enumerate(getattr(citation, field)):
            at = f"{location}.{field}[{position}]"
            if (code := checker.id_rejection(side, kind, value)) is not None:
                yield _reject(code, at)
            elif kind == IdKind.EVIDENCE and checker.detached(side, citation, value):
                yield _reject(RejectionCode.EVIDENCE_DETACHED, at)
            elif kind == IdKind.FINDING and checker.altered(side, value):
                yield _reject(RejectionCode.RESULT_ALTERED, at)


def _check_limitations(proposal: AnswerProposal, checker: _Checker) -> Iterator[Rejection]:
    for number, limitation in enumerate(proposal.limitations):
        at = f"limitations[{number}].finding_id"
        if limitation.side not in checker.indexes:
            yield _reject(RejectionCode.SIDE_UNAVAILABLE, f"limitations[{number}].side")
        elif code := checker.id_rejection(limitation.side, IdKind.FINDING, limitation.finding_id):
            yield _reject(code, at)
