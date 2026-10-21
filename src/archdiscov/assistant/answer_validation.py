"""Validation of an answer proposal against the run that produced it.

Validation checks citations and consistency, not truth: a proposal that passes cites only
records the run actually delivered, for the snapshots the user selected
(`check_citations`), and claims no more than those records support (`check_claims`). Every
rule is structural: whatever cannot be decided from the run record and fingerprints is left
to rendering. Every failure is a `Rejection` with a stable code, the location within the
proposal, and a fixed message that never repeats proposal or tool text, so one repair
request can address them all.
"""

import hashlib
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AnalysisStatus, SnapshotSourceKind
from archdiscov.models.validation_errors import validation_error

from .answers import AnswerProposal, AnswerSide, Citation, ConclusionKind
from .conversation import Contract
from .tools.contracts import IdKind
from .tools.record import RecordedFinding, RunContext, normalize_text
from .tools.session import ToolSession


class RejectionCode(StrEnum):
    SIDE_UNAVAILABLE = "answer_side_unavailable"
    SOURCE_KIND_MISMATCH = "answer_source_kind_mismatch"
    UNKNOWN_ID = "answer_unknown_id"
    OTHER_SNAPSHOT_ID = "answer_other_snapshot_id"
    NOT_RETURNED = "answer_id_not_returned"
    EVIDENCE_DETACHED = "answer_evidence_detached"
    PLANNED_AS_DEPLOYED = "answer_planned_as_deployed"
    STATUS_UPGRADE = "answer_status_upgrade"
    LIMITATION_UNACKNOWLEDGED = "answer_limitation_unacknowledged"
    LIMITATION_UNKNOWN = "answer_limitation_unknown"
    CONTRADICTION_OMITTED = "answer_contradiction_omitted"
    NARRATIVE_IMPORTED_TEXT = "answer_narrative_imported_text"
    NARRATIVE_UNCITED_ID = "answer_narrative_uncited_id"


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
        "This evidence belongs to neither a finding nor a resource cited alongside it."
    ),
    RejectionCode.PLANNED_AS_DEPLOYED: (
        "This snapshot records planned or declared configuration; do not describe it as observed."
    ),
    RejectionCode.STATUS_UPGRADE: "The status is stronger than the cited findings support.",
    RejectionCode.LIMITATION_UNACKNOWLEDGED: (
        "This finding has limitations that the answer does not acknowledge."
    ),
    RejectionCode.LIMITATION_UNKNOWN: "This finding has no limitation with this code.",
    RejectionCode.CONTRADICTION_OMITTED: (
        "A returned finding about the same resource from the same result reaches a "
        "different conclusion and is not cited."
    ),
    RejectionCode.NARRATIVE_IMPORTED_TEXT: (
        "This narrative repeats text imported from cloud configuration or source files."
    ),
    RejectionCode.NARRATIVE_UNCITED_ID: (
        "This narrative names a recorded ID that no conclusion cites."
    ),
}

# Narrative is compared only with imported values and IDs at least this long, so short
# words such as a resource called "web" are not mistaken for repeated text.
MIN_REPEATED_TEXT_CHARS = 24
MIN_NAMED_ID_CHARS = 12

_DECLARED_SOURCES = frozenset(
    {
        SnapshotSourceKind.TERRAFORM_STATE,
        SnapshotSourceKind.TERRAFORM_PLAN,
        SnapshotSourceKind.CLOUDFORMATION,
    }
)
_STATUS_RANK = {
    AnalysisStatus.INSUFFICIENT_EVIDENCE: 0,
    AnalysisStatus.PARTIAL: 1,
    AnalysisStatus.SUPPORTED: 2,
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
    """Classifies cited IDs against one session's snapshots and run record."""

    def __init__(self, session: ToolSession) -> None:
        self.context: RunContext = session.record.context()
        self.indexes = {AnswerSide.PRIMARY: session.index}
        if session.comparison_index is not None:
            self.indexes[AnswerSide.COMPARISON] = session.comparison_index
        self.saved_findings = {
            (result.snapshot_id, finding.id)
            for result in session.analyses
            for finding in result.findings
        }

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
        """Evidence that neither a finding nor the owning resource in the same citation names.

        Evidence about one resource cannot support a claim about another.
        """
        index = self.indexes[side]
        if index.get_evidence(evidence_id).resource_id in citation.resource_ids:
            return False
        return not any(
            evidence_id in finding.evidence_ids
            for finding_id in citation.finding_ids
            if (finding := self.context.finding(index.snapshot_id, finding_id)) is not None
        )


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
        elif (recorded := checker.context.snapshot(index.snapshot_id).source_kind) != (
            item.source_kind
        ):
            presented_as_observed = (
                recorded in _DECLARED_SOURCES and item.source_kind not in _DECLARED_SOURCES
            )
            code = (
                RejectionCode.PLANNED_AS_DEPLOYED
                if presented_as_observed
                else RejectionCode.SOURCE_KIND_MISMATCH
            )
            yield _reject(code, f"contexts[{number}].source_kind")


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


def _check_limitations(proposal: AnswerProposal, checker: _Checker) -> Iterator[Rejection]:
    for number, limitation in enumerate(proposal.limitations):
        at = f"limitations[{number}].finding_id"
        if limitation.side not in checker.indexes:
            yield _reject(RejectionCode.SIDE_UNAVAILABLE, f"limitations[{number}].side")
        elif code := checker.id_rejection(limitation.side, IdKind.FINDING, limitation.finding_id):
            yield _reject(code, at)


# ---- claims ------------------------------------------------------------------------


def check_claims(proposal: AnswerProposal, session: ToolSession) -> tuple[Rejection, ...]:
    """Every failure of the proposal's status, limitations, and narrative, by location."""
    checker = _Checker(session)
    cited = list(_cited_findings(proposal, checker))
    rejections = [
        *_check_status(proposal, cited),
        *_check_acknowledgements(proposal, cited, checker),
        *_check_contradictions(cited, checker),
        *_check_narrative(proposal, checker),
    ]
    return tuple(sorted(set(rejections), key=lambda item: (item.location, item.code)))


def validate_proposal(proposal: AnswerProposal, session: ToolSession) -> tuple[Rejection, ...]:
    """Citation and claim failures together; an empty result means the proposal is valid."""
    rejections = {*check_citations(proposal, session), *check_claims(proposal, session)}
    return tuple(sorted(rejections, key=lambda item: (item.location, item.code)))


def _cited_findings(
    proposal: AnswerProposal, checker: _Checker
) -> Iterator[tuple[str, AnswerSide, RecordedFinding]]:
    """Each cited finding the run delivered, with its location; others fail citations."""
    for number, conclusion in enumerate(proposal.conclusions):
        for position, citation in enumerate(conclusion.citations):
            index = checker.indexes.get(citation.side)
            if index is None:
                continue
            for finding_position, finding_id in enumerate(citation.finding_ids):
                finding = checker.context.finding(index.snapshot_id, finding_id)
                if finding is not None:
                    location = (
                        f"conclusions[{number}].citations[{position}]"
                        f".finding_ids[{finding_position}]"
                    )
                    yield location, citation.side, finding


def _supported_status(
    proposal: AnswerProposal, cited: list[tuple[str, AnswerSide, RecordedFinding]]
) -> AnalysisStatus:
    """The strongest status the cited records allow.

    Findings decide it; without findings, recorded differences allow at most `partial`, since
    their qualifications are not checked here, and configuration alone is only context.
    """
    if not cited:
        differences = any(
            conclusion.kind == ConclusionKind.RECORDED_DIFFERENCE
            for conclusion in proposal.conclusions
        )
        return AnalysisStatus.PARTIAL if differences else AnalysisStatus.INSUFFICIENT_EVIDENCE
    statuses = {finding.status for _, _, finding in cited}
    if statuses == {AnalysisStatus.INSUFFICIENT_EVIDENCE}:
        return AnalysisStatus.INSUFFICIENT_EVIDENCE
    if statuses == {AnalysisStatus.SUPPORTED}:
        return AnalysisStatus.SUPPORTED
    return AnalysisStatus.PARTIAL


def _check_status(
    proposal: AnswerProposal, cited: list[tuple[str, AnswerSide, RecordedFinding]]
) -> Iterator[Rejection]:
    if _STATUS_RANK[proposal.status] > _STATUS_RANK[_supported_status(proposal, cited)]:
        yield _reject(RejectionCode.STATUS_UPGRADE, "status")


def _check_acknowledgements(
    proposal: AnswerProposal,
    cited: list[tuple[str, AnswerSide, RecordedFinding]],
    checker: _Checker,
) -> Iterator[Rejection]:
    acknowledged = {(item.side, item.finding_id, item.limitation) for item in proposal.limitations}
    for location, side, finding in cited:
        if any(
            (side, finding.finding_id, code) not in acknowledged
            for code in finding.limitation_codes
        ):
            yield _reject(RejectionCode.LIMITATION_UNACKNOWLEDGED, location)
    for number, item in enumerate(proposal.limitations):
        index = checker.indexes.get(item.side)
        finding = (
            None if index is None else checker.context.finding(index.snapshot_id, item.finding_id)
        )
        if finding is not None and item.limitation not in finding.limitation_codes:
            yield _reject(RejectionCode.LIMITATION_UNKNOWN, f"limitations[{number}].limitation")


def _check_contradictions(
    cited: list[tuple[str, AnswerSide, RecordedFinding]], checker: _Checker
) -> Iterator[Rejection]:
    """A cited finding whose returned sibling (same result, same primary resource) concludes
    differently must not be cited without that sibling."""
    cited_keys = {(finding.snapshot_id, finding.finding_id) for _, _, finding in cited}
    for location, _, finding in cited:
        if finding.primary_resource_id is None:
            continue
        for other in checker.context.findings:
            if (
                (other.snapshot_id, other.finding_id) not in cited_keys
                and other.snapshot_id == finding.snapshot_id
                and other.primary_resource_id == finding.primary_resource_id
                and set(other.result_keys) & set(finding.result_keys)
                and (other.status, other.decision) != (finding.status, finding.decision)
            ):
                yield _reject(RejectionCode.CONTRADICTION_OMITTED, location)
                break


def _narrative(proposal: AnswerProposal) -> Iterator[tuple[str, str]]:
    yield "summary", proposal.summary
    if proposal.explanation is not None:
        yield "explanation", proposal.explanation


def _check_narrative(proposal: AnswerProposal, checker: _Checker) -> Iterator[Rejection]:
    repeated = _fingerprints_by_length(checker.context)
    named = _uncited_ids(proposal, checker)
    for location, text in _narrative(proposal):
        if _repeats_imported_text(text, repeated):
            yield _reject(RejectionCode.NARRATIVE_IMPORTED_TEXT, location)
        if any(value in text for value in named):
            yield _reject(RejectionCode.NARRATIVE_UNCITED_ID, location)


def _fingerprints_by_length(context: RunContext) -> dict[int, set[str]]:
    lengths: dict[int, set[str]] = {}
    for item in context.fingerprints:
        if item.length >= MIN_REPEATED_TEXT_CHARS:
            lengths.setdefault(item.length, set()).add(item.digest)
    return lengths


def _repeats_imported_text(text: str, lengths: dict[int, set[str]]) -> bool:
    """Whether any window of the normalized text has a delivered value's fingerprint."""
    normalized = normalize_text(text)
    return any(
        hashlib.sha256(normalized[start : start + length].encode()).hexdigest() in digests
        for length, digests in lengths.items()
        for start in range(len(normalized) - length + 1)
    )


def _uncited_ids(proposal: AnswerProposal, checker: _Checker) -> set[str]:
    """Recorded IDs long enough to recognize in text that no conclusion cites."""
    cited = {
        value
        for conclusion in proposal.conclusions
        for citation in conclusion.citations
        for field, _ in _CITED
        for value in getattr(citation, field)
    }
    known = {finding.finding_id for finding in checker.context.findings}
    for index in checker.indexes.values():
        known |= set(index.resource_ids) | set(index.evidence_ids)
    return {value for value in known - cited if len(value) >= MIN_NAMED_ID_CHARS}
