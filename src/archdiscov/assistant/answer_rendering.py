"""Rendering a validated proposal as a `RenderedAnswer`, with every fact taken from records.

The proposal decides only which records are shown. Their facts come from the finding views
tools delivered to the model, the evidence and resources in the session's indexes, and the
saved comparisons the user selected. Each statement is a fixed template; record values
(summaries, names, paths, configuration values) are quoted in the conclusion's `data` and
referred to as `[n]`, so a model cannot change how a fact is worded. The narrative appears,
with secret-looking text redacted, only as the labelled explanation, never as a conclusion.
"""

from collections.abc import Iterable, Iterator, Mapping

from archdiscov.graph.identity import native_resource_type
from archdiscov.models.analysis.references import ResourceReference
from archdiscov.models.comparison.entries import (
    AmbiguousMatch,
    FieldDifference,
    MatchedPair,
    Qualification,
    UnmatchedResource,
)
from archdiscov.models.comparison.results import ComparisonResult
from archdiscov.models.enums import (
    AnalysisStatus,
    ComparisonSide,
    EvidenceValueStatus,
    SnapshotSourceKind,
)

from .answer_results import (
    MAX_RENDERED_ENTRIES,
    AnswerOutcome,
    AnswerStatus,
    RenderedAnswer,
    RenderedConclusion,
    RenderedNarrative,
    RenderedSnapshot,
    Statement,
)
from .answer_validation import side_indexes
from .answers import (
    DECLARED_SOURCE_KINDS,
    AnswerProposal,
    AnswerSide,
    Citation,
    Conclusion,
    ConclusionKind,
    InsufficientReason,
    combined_status,
)
from .redaction import redact_secrets
from .tools.contracts import UntrustedText, untrusted
from .tools.record import RunContext
from .tools.session import ToolSession
from .tools.views import FindingView, evidence_view, json_text

type _Entry = MatchedPair | UnmatchedResource | AmbiguousMatch
type _Sides = Mapping[ComparisonSide, AnswerSide]

_FINDING_STATUS = {
    AnalysisStatus.SUPPORTED: "reports a supported finding",
    AnalysisStatus.PARTIAL: "reports a partial finding, which its limitations qualify",
    AnalysisStatus.INSUFFICIENT_EVIDENCE: "found insufficient evidence to conclude",
}

_EVIDENCE_VALUE = {
    EvidenceValueStatus.PRESENT: "records the value {value} at the configuration path {path}",
    EvidenceValueStatus.REDACTED: "records a redacted value at the configuration path {path}",
    EvidenceValueStatus.OMITTED: "records that the value at the configuration path {path} "
    "was omitted",
    EvidenceValueStatus.UNRESOLVED: "records that the value at the configuration path {path} "
    "could not be resolved",
}

_DECLARED = {
    SnapshotSourceKind.TERRAFORM_STATE: "is Terraform state: declared configuration, "
    "not observed deployed state",
    SnapshotSourceKind.TERRAFORM_PLAN: "is a Terraform plan: planned changes, not deployed state",
    SnapshotSourceKind.CLOUDFORMATION: "is a CloudFormation template: declared configuration, "
    "not deployed state",
}

# What an insufficient answer checked, never what is true.
INSUFFICIENT_REASONS = {
    InsufficientReason.NO_APPLICABLE_FINDINGS: "No finding in the records checked applies to the "
    "question; this does not show that none exists.",
    InsufficientReason.LIMITED_COVERAGE: "The records checked were collected with limited "
    "coverage, so they cannot settle the question.",
    InsufficientReason.UNSUPPORTED_ANALYSIS: "No available analyzer checks what the question "
    "asks, so the records checked cannot settle it.",
    InsufficientReason.OUT_OF_SCOPE: "The question is outside what the selected snapshots "
    "record, so the records checked cannot settle it.",
}

PARTIAL_ANSWER = "The answer is partial: the cited records support it only in part."
QUALIFIED_FINDING = (
    "At least one cited finding is not fully supported; read it with its limitations."
)
LIMITED_COVERAGE = "Collection coverage was incomplete for at least one cited finding."
RECORDED_DIFFERENCES = (
    "Differences come from a saved comparison of recorded state; they are not proof of change "
    "or drift."
)
NO_COMPARISON_ENTRY = "No saved comparison entry names the cited resources."
MISSING_DIFFERENCE = (
    "At least one cited difference has no saved comparison entry naming its resources."
)


def render_answer(
    proposal: AnswerProposal, session: ToolSession, outcome: AnswerOutcome
) -> RenderedAnswer:
    """The answer for a proposal that passed `validate_proposal` against this session."""
    if outcome.status == AnswerStatus.FAILED:
        raise ValueError("a failed run is rendered as a failure, not an answer")
    renderer = _Renderer(session)
    conclusions = tuple(renderer.conclusion(item) for item in proposal.conclusions)
    snapshots = rendered_snapshots(session)
    return RenderedAnswer(
        outcome=outcome,
        snapshots=snapshots,
        status=proposal.status,
        conclusions=conclusions,
        qualifications=(
            *_declared(snapshots),
            *_qualified(proposal.status, conclusions),
            *_insufficient(proposal, renderer.context),
        ),
        narrative=RenderedNarrative(
            summary=redact_secrets(proposal.summary),
            explanation=None
            if proposal.explanation is None
            else redact_secrets(proposal.explanation),
        ),
    )


def rendered_snapshots(session: ToolSession) -> tuple[RenderedSnapshot, ...]:
    """The snapshot behind each side, primary first, as the run recorded and validated it."""
    context = session.record.context()
    snapshots = []
    for side, index in side_indexes(session).items():
        recorded = context.snapshot(index.snapshot_id)
        if recorded is None:
            raise ValueError("a selected snapshot was not recorded for the run")
        snapshots.append(
            RenderedSnapshot(
                side=side,
                declared=recorded.source_kind in DECLARED_SOURCE_KINDS,
                **recorded.model_dump(),
            )
        )
    return tuple(snapshots)


class _Quoted:
    """Statements under construction and the record values they quote, numbered from 1."""

    def __init__(self) -> None:
        self.statements: list[Statement] = []
        self.data: list[UntrustedText] = []

    def quote(self, value: UntrustedText) -> str:
        self.data.append(value)
        return f"[{len(self.data)}]"

    def say(self, statement: str) -> None:
        self.statements.append(statement)


class _Renderer:
    def __init__(self, session: ToolSession) -> None:
        self.context = session.record.context()
        self.indexes = side_indexes(session)
        self.comparisons = session.comparisons

    def conclusion(self, conclusion: Conclusion) -> RenderedConclusion:
        """Every cited finding, evidence item, and resource, then the recorded differences."""
        text = _Quoted()
        findings = []
        for citation in conclusion.citations:
            findings += self._findings(citation, text)
            self._evidence(citation, text)
            self._resources(citation, text)
        if conclusion.kind == ConclusionKind.RECORDED_DIFFERENCE:
            self._differences(conclusion.citations, text)
        return RenderedConclusion(
            kind=conclusion.kind,
            statements=tuple(text.statements),
            data=tuple(text.data),
            findings=tuple(findings),
            status=combined_status(view.status for view in findings) if findings else None,
            citations=conclusion.citations,
        )

    def _findings(self, citation: Citation, text: _Quoted) -> list[FindingView]:
        snapshot_id = self.indexes[citation.side].snapshot_id
        views = []
        for finding_id in citation.finding_ids:
            view = self.context.view(snapshot_id, finding_id)
            recorded = self.context.finding(snapshot_id, finding_id)
            if view is None or recorded is None:
                raise ValueError("a cited finding was not delivered in this run")
            views.append(view)
            limitations = (
                f" It lists {len(view.limitations)} limitation(s)." if view.limitations else ""
            )
            text.say(
                f"Finding {len(views)} on the {citation.side} snapshot: the {recorded.analyzer} "
                f"analyzer {_FINDING_STATUS[view.status]}; its summary is quoted as "
                f"{text.quote(view.summary)}.{limitations}"
            )
        return views

    def _evidence(self, citation: Citation, text: _Quoted) -> None:
        index = self.indexes[citation.side]
        for number, evidence_id in enumerate(citation.evidence_ids, start=1):
            evidence = index.get_evidence(evidence_id)
            if evidence is None:
                raise ValueError("cited evidence is not in the snapshot")
            view = evidence_view(evidence_id, evidence)
            path = text.quote(view.configuration_path)
            value = "" if view.value is None else text.quote(view.value)
            recorded = _EVIDENCE_VALUE[view.value_status].format(path=path, value=value)
            text.say(
                f"Evidence {number} on the {citation.side} snapshot, from the {view.source} "
                f"source, {recorded}."
            )

    def _resources(self, citation: Citation, text: _Quoted) -> None:
        index = self.indexes[citation.side]
        for number, resource_id in enumerate(citation.resource_ids, start=1):
            resource = index.get_resource(resource_id)
            if resource is None:
                raise ValueError("a cited resource is not in the snapshot")
            kind = text.quote(untrusted(native_resource_type(resource)))
            name = (
                "with no recorded name"
                if resource.name is None
                else f"named {text.quote(untrusted(resource.name))}"
            )
            planned = (
                ""
                if resource.lifecycle is None
                else " Its planned actions are "
                + ", ".join(action.value for action in resource.lifecycle.actions)
                + ", not deployed changes."
            )
            text.say(
                f"Resource {number} on the {citation.side} snapshot is recorded as {kind} "
                f"{name}.{planned}"
            )

    def _differences(self, citations: tuple[Citation, ...], text: _Quoted) -> None:
        """The saved comparison entries naming a cited resource, up to the rendering bounds."""
        cited = {citation.side: set(citation.resource_ids) for citation in citations}
        entries = [
            (sides, entry)
            for result in self.comparisons
            if (sides := self._sides(result)) is not None
            for entry in _naming(result, sides, cited)
        ]
        if not entries:
            text.say(NO_COMPARISON_ENTRY)
        budget = _Budget(MAX_RENDERED_ENTRIES)
        for sides, entry in entries[:MAX_RENDERED_ENTRIES]:
            _ENTRY_STATEMENTS[type(entry)](entry, sides, text, budget)
        if (hidden := len(entries) - MAX_RENDERED_ENTRIES) > 0:
            text.say(f"{hidden} more comparison entries are not shown.")
        if budget.hidden:
            text.say(f"{budget.hidden} more field differences are not shown.")

    def _sides(self, result: ComparisonResult) -> _Sides | None:
        """Which answer side each side of a saved comparison is, if it compares the two.

        A snapshot compared with itself is read in selection order: left is primary.
        """
        primary = self.indexes[AnswerSide.PRIMARY].snapshot_id
        comparison = self.indexes[AnswerSide.COMPARISON].snapshot_id
        compared = (result.left.snapshot_id, result.right.snapshot_id)
        if compared == (primary, comparison):
            return {
                ComparisonSide.LEFT: AnswerSide.PRIMARY,
                ComparisonSide.RIGHT: AnswerSide.COMPARISON,
            }
        if compared == (comparison, primary):
            return {
                ComparisonSide.LEFT: AnswerSide.COMPARISON,
                ComparisonSide.RIGHT: AnswerSide.PRIMARY,
            }
        return None


# ---- comparison entries --------------------------------------------------------------


class _Budget:
    """How many field differences may still be shown, and how many were not."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.shown = 0
        self.hidden = 0

    def take(self) -> bool:
        if self.shown < self.limit:
            self.shown += 1
            return True
        self.hidden += 1
        return False


def _naming(
    result: ComparisonResult, sides: _Sides, cited: Mapping[AnswerSide, set[str]]
) -> list[_Entry]:
    """The entries of a saved comparison that name a cited resource on its own side."""
    on = {side: cited[answer_side] for side, answer_side in sides.items()}

    def named(side: ComparisonSide, references: Iterable[ResourceReference]) -> bool:
        return any(reference.resource_id in on[side] for reference in references)

    return [
        *(
            pair
            for pair in result.matched
            if named(ComparisonSide.LEFT, [pair.left]) or named(ComparisonSide.RIGHT, [pair.right])
        ),
        *(item for item in result.unmatched if named(item.side, [item.resource])),
        *(
            item
            for item in result.ambiguous
            if named(ComparisonSide.LEFT, item.left) or named(ComparisonSide.RIGHT, item.right)
        ),
    ]


def _matched(pair: MatchedPair, sides: _Sides, text: _Quoted, budget: _Budget) -> None:
    left, right = sides[ComparisonSide.LEFT], sides[ComparisonSide.RIGHT]
    text.say(
        f"The saved comparison matched a resource on the {left} snapshot with one on the "
        f"{right} snapshot by {pair.basis}, recording {len(pair.differences)} field "
        f"difference(s).{_qualified_by(pair.qualifications)}"
    )
    for item in pair.differences:
        if budget.take():
            text.say(_difference(item, budget.shown, sides, text))


def _difference(item: FieldDifference, number: int, sides: _Sides, text: _Quoted) -> str:
    path = text.quote(untrusted(item.path))
    left = text.quote(json_text(item.left_value))
    right = text.quote(json_text(item.right_value))
    return (
        f"Difference {number} ({item.kind}) at {path}: the {sides[ComparisonSide.LEFT]} "
        f"snapshot records {left} and the {sides[ComparisonSide.RIGHT]} snapshot records "
        f"{right}.{_qualified_by(item.qualifications)}"
    )


def _unmatched(item: UnmatchedResource, sides: _Sides, text: _Quoted, _: _Budget) -> None:
    text.say(
        f"The saved comparison found no match on the {sides[item.side.opposite]} snapshot for "
        f"a resource on the {sides[item.side]} snapshot.{_qualified_by(item.qualifications)}"
    )


def _ambiguous(item: AmbiguousMatch, sides: _Sides, text: _Quoted, _: _Budget) -> None:
    text.say(
        f"The saved comparison could not match by {item.basis}: {len(item.left)} resource(s) "
        f"on the {sides[ComparisonSide.LEFT]} snapshot and {len(item.right)} on the "
        f"{sides[ComparisonSide.RIGHT]} snapshot share the key {text.quote(untrusted(item.key))}."
    )


_ENTRY_STATEMENTS = {
    MatchedPair: _matched,
    UnmatchedResource: _unmatched,
    AmbiguousMatch: _ambiguous,
}


def _qualified_by(qualifications: Iterable[Qualification]) -> str:
    kinds = sorted({item.kind.value for item in qualifications})
    return f" It is qualified by {', '.join(kinds)}." if kinds else ""


# ---- answer-wide qualifications -----------------------------------------------------


def _declared(snapshots: Iterable[RenderedSnapshot]) -> Iterator[str]:
    for snapshot in snapshots:
        if snapshot.declared:
            yield f"The {snapshot.side} snapshot {_DECLARED[snapshot.source_kind]}."


def _qualified(
    status: AnalysisStatus, conclusions: tuple[RenderedConclusion, ...]
) -> Iterator[str]:
    views = [view for conclusion in conclusions for view in conclusion.findings]
    if status == AnalysisStatus.PARTIAL:
        yield PARTIAL_ANSWER
    if any(view.status != AnalysisStatus.SUPPORTED for view in views):
        yield QUALIFIED_FINDING
    if any(item.service is not None for view in views for item in view.limitations):
        yield LIMITED_COVERAGE
    differences = [c for c in conclusions if c.kind == ConclusionKind.RECORDED_DIFFERENCE]
    if differences:
        yield RECORDED_DIFFERENCES
    if any(NO_COMPARISON_ENTRY in conclusion.statements for conclusion in differences):
        yield MISSING_DIFFERENCE


def _insufficient(proposal: AnswerProposal, context: RunContext) -> Iterator[str]:
    """What an insufficient answer checked, so it never reads as a finding."""
    if proposal.insufficient_reason is None:
        return
    yield INSUFFICIENT_REASONS[proposal.insufficient_reason]
    analyzers = sorted({finding.analyzer.value for finding in context.findings})
    if analyzers:
        yield (
            f"The run retrieved {len(context.findings)} finding(s) from these analyzers: "
            f"{', '.join(analyzers)}."
        )
    else:
        yield "The run retrieved no analyzer findings."
