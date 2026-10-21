"""Analyzer and saved-result tools.

`run_analyzer` runs one of the five analyzers over the session's snapshot through
`run_analyzer` and `validate_analysis_result`, as the `analyze` command does before
saving, but in memory: no tool writes files. Saved analysis results and comparisons are
read only when the user selected them for the session, and the session verified them
against their snapshots when it was created. Findings keep their citations, paths,
limitations, and analyzer-specific blocks (which hold only enums, numbers, and IDs);
summaries and other recorded text are marked data.
"""

from collections import Counter
from typing import Annotated, Any
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, PrivateAttr

from archdiscov.models.analysis.database_access import DatabaseAccess
from archdiscov.models.analysis.exposure import InternetExposure
from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.parameters import AnalyzerParameters
from archdiscov.models.analysis.references import AnalysisLimitation, PathStep
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.analysis.trust import CrossAccountTrust
from archdiscov.models.canonical import sha256_json
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
    AnalyzerErrorCode,
    AnalyzerKind,
    ComparisonLimitationKind,
    ComparisonSide,
    DependencyRelationship,
    SnapshotSourceKind,
)
from archdiscov.models.resource_base import Model, NonEmptyString
from archdiscov.services.analysis_execution import DEFAULT_REGISTRY, run_analyzer
from archdiscov.services.analysis_validation import (
    AnalysisValidationError,
    validate_analysis_result,
)
from archdiscov.services.analyzer_errors import AnalyzerError

from .contracts import (
    BoundedList,
    DeliveredAnalysis,
    ToolError,
    ToolErrorCode,
    ToolOutput,
    UntrustedText,
    tool_error,
    untrusted,
)
from .registry import EmptyInput, ToolDefinition, ToolInput
from .session import ToolSession
from .views import (
    AttributeView,
    ResourceRef,
    TargetView,
    capped,
    fit,
    json_text,
    target_view,
)

PARAMETER_MODELS = {
    model.model_fields["kind"].default: model for model in AnalyzerParameters.__args__[0].__args__
}
# The analyzers' own maximum, so tools can count the findings they drop.
_ANALYZER_RESULT_LIMIT = 10_000
_ANALYZER_SUMMARIES = {
    AnalyzerKind.OWNERSHIP: "Resources with no usable owner tag.",
    AnalyzerKind.SECURITY_GROUP_IMPACT: (
        "What is attached to, references, or depends on one security group."
    ),
    AnalyzerKind.CROSS_ACCOUNT_TRUST: (
        "Principals that IAM role trust policies and SQS queue policies are configured to trust."
    ),
    AnalyzerKind.DATABASE_ACCESS: (
        "Compute sources with a recorded potential network path to one database port."
    ),
    AnalyzerKind.INTERNET_EXPOSURE: (
        "Entry points that could expose a service to the internet, from recorded configuration."
    ),
}

Limit = Annotated[int, Field(default=25, ge=1, le=100, strict=True)]
Number = Annotated[int, Field(ge=1, le=1_000, strict=True, description="1-based position.")]
Text = Annotated[NonEmptyString, Field(max_length=1_024)]


# ---- finding views -----------------------------------------------------------------


class PathStepView(Model):
    kind: str
    source: ResourceRef
    target: TargetView
    relationship: DependencyRelationship | None = None
    evidence_ids: tuple[str, ...]
    attributes: tuple[AttributeView, ...] = ()


def _step_view(step: PathStep) -> PathStepView:
    if step.kind == "containment":
        return PathStepView(
            kind="containment",
            source=ResourceRef(resource_id=step.source_id),
            target=TargetView(kind="local", resource_id=step.target_id),
            evidence_ids=tuple(step.evidence_ids),
        )
    return PathStepView(
        kind="dependency",
        source=ResourceRef(resource_id=step.source_id),
        target=target_view(step.target),
        relationship=step.relationship,
        evidence_ids=tuple(step.evidence_ids),
        attributes=tuple(
            AttributeView(key=untrusted(key), value=json_text(value))
            for key, value in sorted(step.attributes.items())
        ),
    )


class AnalysisLimitationView(Model):
    code: str
    message: UntrustedText
    service: str | None = None
    operation: str | None = None
    location: str | None = None
    unresolved_source: ResourceRef | None = None
    unresolved_target: TargetView | None = None
    evidence_ids: tuple[str, ...] = ()


def _limitation_view(limitation: AnalysisLimitation) -> AnalysisLimitationView:
    coverage = limitation.coverage
    unresolved = limitation.unresolved_target
    return AnalysisLimitationView(
        code=limitation.code,
        message=untrusted(limitation.message),
        service=None if coverage is None else coverage.service,
        operation=None if coverage is None else coverage.operation,
        location=None if coverage is None else coverage.location,
        unresolved_source=None
        if unresolved is None
        else ResourceRef(resource_id=unresolved.source_id),
        unresolved_target=None if unresolved is None else target_view(unresolved.target),
        evidence_ids=tuple(limitation.evidence_ids)
        + (() if unresolved is None else tuple(unresolved.evidence_ids)),
    )


class FindingView(Model):
    finding_id: str
    status: AnalysisStatus
    summary: UntrustedText
    resources: tuple[ResourceRef, ...]
    evidence_ids: tuple[str, ...]
    path: tuple[PathStepView, ...]
    limitations: tuple[AnalysisLimitationView, ...]
    impact: SecurityGroupImpact | None = None
    trust: CrossAccountTrust | None = None
    database_access: DatabaseAccess | None = None
    internet_exposure: InternetExposure | None = None


def _finding_view(finding: AnalysisFinding) -> FindingView:
    return FindingView(
        finding_id=finding.id,
        status=finding.status,
        summary=untrusted(finding.summary),
        resources=tuple(ResourceRef(resource_id=ref.resource_id) for ref in finding.resources),
        evidence_ids=tuple(finding.evidence_ids),
        path=tuple(_step_view(step) for step in finding.path),
        limitations=tuple(_limitation_view(item) for item in finding.limitations),
        impact=finding.impact,
        trust=finding.trust,
        database_access=finding.database_access,
        internet_exposure=finding.internet_exposure,
    )


class AnalysisOutput(ToolOutput):
    """Findings of one analyzer over one snapshot; `saved` is false for in-memory runs.

    The result behind the output is attached privately for the run record (so two outputs
    with equal fields compare unequal when their results differ).
    """

    snapshot_id: UUID
    analyzer: AnalyzerKind
    analyzer_version: str
    parameters: dict[str, JsonValue]
    saved: bool
    created_at: AwareDatetime
    status_counts: dict[AnalysisStatus, int]
    findings: BoundedList[FindingView]
    _delivered: DeliveredAnalysis | None = PrivateAttr(default=None)

    def delivered_analysis(self) -> DeliveredAnalysis | None:
        return self._delivered


def _run_key(result: AnalysisResult) -> str:
    """Identical in-memory runs share a key; it is not a saved-result number."""
    parameters = sha256_json(result.parameters.model_dump(mode="json"))[:16]
    return f"run:{result.analyzer}:{result.analyzer_version}:{parameters}"


def _analysis_output(
    session: ToolSession, result: AnalysisResult, requested: int, *, result_key: str
) -> AnalysisOutput:
    counts = Counter(finding.status for finding in result.findings)
    views = [_finding_view(finding) for finding in result.findings]

    def build(effective: int) -> AnalysisOutput:
        return AnalysisOutput(
            snapshot_id=result.snapshot_id,
            analyzer=result.analyzer,
            analyzer_version=result.analyzer_version,
            parameters=result.parameters.model_dump(mode="json"),
            saved=result_key.startswith("saved:"),
            created_at=result.created_at,
            status_counts={status: counts[status] for status in AnalysisStatus},
            findings=capped(views, requested, effective),
        )

    output = fit(build, requested, session.limits.max_output_chars)
    output._delivered = DeliveredAnalysis(
        result_key=result_key,
        result=result,
        finding_ids=tuple(view.finding_id for view in output.findings.items),
    )
    return output


# ---- list and run ------------------------------------------------------------------


class ParameterView(Model):
    name: str
    required: bool
    default: JsonValue = None
    minimum: int | None = None
    maximum: int | None = None


class AnalyzerView(Model):
    kind: AnalyzerKind
    version: str
    summary: str
    parameters: tuple[ParameterView, ...]


class AnalyzersOutput(ToolOutput):
    snapshot_id: UUID
    source_kind: SnapshotSourceKind
    collection_started_at: AwareDatetime
    collection_completed_at: AwareDatetime
    analyzers: tuple[AnalyzerView, ...]


def _tool_parameters(kind: AnalyzerKind) -> dict[str, Any]:
    """The analyzer's parameter fields that `run_analyzer` accepts (not `kind`, not limits)."""
    fields = PARAMETER_MODELS[kind].model_fields
    return {name: field for name, field in fields.items() if name not in {"kind", "result_limit"}}


def _bound(field, attribute: str) -> int | None:
    return next(
        (getattr(item, attribute) for item in field.metadata if hasattr(item, attribute)), None
    )


def _list_analyzers(session: ToolSession, _: EmptyInput) -> AnalyzersOutput:
    analyzers = []
    for kind in AnalyzerKind:
        parameters = []
        for name, field in _tool_parameters(kind).items():
            default = None if field.is_required() else field.get_default(call_default_factory=True)
            maximum = _bound(field, "le")
            if name == "max_depth":
                maximum = min(maximum, session.limits.depth)
                default = min(default, maximum)
            parameters.append(
                ParameterView(
                    name=name,
                    required=field.is_required(),
                    default=default,
                    minimum=_bound(field, "ge"),
                    maximum=maximum,
                )
            )
        analyzers.append(
            AnalyzerView(
                kind=kind,
                version=DEFAULT_REGISTRY.get(kind).version,
                summary=_ANALYZER_SUMMARIES[kind],
                parameters=tuple(parameters),
            )
        )
    started, completed = session.index.collection_window
    return AnalyzersOutput(
        snapshot_id=session.index.snapshot_id,
        source_kind=session.index.source_kind,
        collection_started_at=started,
        collection_completed_at=completed,
        analyzers=tuple(analyzers),
    )


class RunAnalyzerInput(ToolInput):
    """Parameters an analyzer does not accept are rejected; see `list_analyzers`."""

    kind: AnalyzerKind
    resource_id: Text | None = None
    port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    max_depth: int | None = Field(default=None, ge=1, le=10, strict=True)
    ownership_keys: tuple[Text, ...] | None = Field(default=None, min_length=1, max_length=20)
    limit: Limit = 25


_ANALYZER_ERRORS = {
    AnalyzerErrorCode.RESOURCE_NOT_FOUND: ToolErrorCode.NOT_FOUND,
    AnalyzerErrorCode.UNSUPPORTED_RESOURCE: ToolErrorCode.INVALID_ARGUMENTS,
    AnalyzerErrorCode.INVALID_PARAMETERS: ToolErrorCode.INVALID_ARGUMENTS,
    AnalyzerErrorCode.UNSUPPORTED_ANALYZER: ToolErrorCode.INVALID_ARGUMENTS,
    AnalyzerErrorCode.INVALID_RESULT: ToolErrorCode.INTERNAL_ERROR,
}


def _run(session: ToolSession, arguments: RunAnalyzerInput) -> AnalysisOutput | ToolError:
    accepted = _tool_parameters(arguments.kind)
    supplied = {
        name: getattr(arguments, name)
        for name in ("resource_id", "port", "max_depth", "ownership_keys")
        if getattr(arguments, name) is not None
    }
    unaccepted = sorted(set(supplied) - set(accepted))
    missing = sorted(
        name for name, field in accepted.items() if field.is_required() and name not in supplied
    )
    if unaccepted or missing:
        return tool_error(ToolErrorCode.INVALID_ARGUMENTS, unaccepted + missing)
    parameters: dict[str, Any] = {"kind": arguments.kind, **supplied}
    if "ownership_keys" in parameters:
        parameters["ownership_keys"] = list(parameters["ownership_keys"])
    if "max_depth" in accepted:
        default = accepted["max_depth"].default
        parameters["max_depth"] = min(parameters.get("max_depth", default), session.limits.depth)
    if "result_limit" in PARAMETER_MODELS[arguments.kind].model_fields:
        parameters["result_limit"] = _ANALYZER_RESULT_LIMIT
    try:
        result = run_analyzer(session.index, parameters)
        result = validate_analysis_result(result, session.snapshot(session.index.snapshot_id))
    except AnalyzerError as error:
        return tool_error(_ANALYZER_ERRORS[error.code])
    except AnalysisValidationError:
        return tool_error(ToolErrorCode.INTERNAL_ERROR)
    requested = min(arguments.limit, session.limits.result_limit)
    return _analysis_output(session, result, requested, result_key=_run_key(result))


# ---- saved results -----------------------------------------------------------------


class SavedAnalysisView(Model):
    number: int
    snapshot_id: UUID
    analyzer: AnalyzerKind
    analyzer_version: str
    parameters: dict[str, JsonValue]
    created_at: AwareDatetime
    finding_count: int


class SavedComparisonView(Model):
    number: int
    left_snapshot_id: UUID
    right_snapshot_id: UUID
    created_at: AwareDatetime
    matched_count: int
    unmatched_count: int
    ambiguous_count: int


class SavedResultsOutput(ToolOutput):
    """Results the user selected for this session; the model cannot add others."""

    analyses: tuple[SavedAnalysisView, ...]
    comparisons: tuple[SavedComparisonView, ...]


def _list_saved(session: ToolSession, _: EmptyInput) -> SavedResultsOutput:
    return SavedResultsOutput(
        analyses=tuple(
            SavedAnalysisView(
                number=number,
                snapshot_id=result.snapshot_id,
                analyzer=result.analyzer,
                analyzer_version=result.analyzer_version,
                parameters=result.parameters.model_dump(mode="json"),
                created_at=result.created_at,
                finding_count=len(result.findings),
            )
            for number, result in enumerate(session.analyses, start=1)
        ),
        comparisons=tuple(
            SavedComparisonView(
                number=number,
                left_snapshot_id=result.left.snapshot_id,
                right_snapshot_id=result.right.snapshot_id,
                created_at=result.created_at,
                matched_count=len(result.matched),
                unmatched_count=len(result.unmatched),
                ambiguous_count=len(result.ambiguous),
            )
            for number, result in enumerate(session.comparisons, start=1)
        ),
    )


class SavedResultInput(ToolInput):
    number: Number
    limit: Limit = 25


def _get_saved_analysis(
    session: ToolSession, arguments: SavedResultInput
) -> AnalysisOutput | ToolError:
    if not session.analyses:
        return tool_error(ToolErrorCode.UNAVAILABLE)
    if arguments.number > len(session.analyses):
        return tool_error(ToolErrorCode.NOT_FOUND)
    requested = min(arguments.limit, session.limits.result_limit)
    return _analysis_output(
        session,
        session.analyses[arguments.number - 1],
        requested,
        result_key=f"saved:{arguments.number}",
    )


class SideRef(Model):
    """A resource on one side of a comparison, scoped to that side's snapshot."""

    snapshot_id: UUID
    resource_id: str


class SideEvidence(Model):
    snapshot_id: UUID
    evidence_ids: tuple[str, ...]


class QualificationView(Model):
    kind: str
    side: ComparisonSide
    service: str | None = None
    operation: str | None = None
    lifecycle_actions: tuple[str, ...] = ()
    evidence: SideEvidence


class DifferenceView(Model):
    subject: str
    path: UntrustedText
    kind: str
    left_value: UntrustedText
    right_value: UntrustedText
    left: SideEvidence
    right: SideEvidence
    qualifications: tuple[QualificationView, ...]


class MatchedView(Model):
    entry_id: str
    basis: str
    left: SideRef
    right: SideRef
    differences: tuple[DifferenceView, ...]
    qualifications: tuple[QualificationView, ...]


class UnmatchedView(Model):
    entry_id: str
    side: ComparisonSide
    resource: SideRef
    qualifications: tuple[QualificationView, ...]


class AmbiguousView(Model):
    entry_id: str
    basis: str
    key: UntrustedText
    left: tuple[SideRef, ...]
    right: tuple[SideRef, ...]


class ComparisonSideView(Model):
    snapshot_id: UUID
    source_kind: SnapshotSourceKind
    collection_started_at: AwareDatetime
    collection_completed_at: AwareDatetime


class ComparisonLimitationView(Model):
    kind: ComparisonLimitationKind
    message: UntrustedText


class ComparisonOutput(ToolOutput):
    """Recorded differences between two selected snapshots; not proof of change or drift."""

    left: ComparisonSideView
    right: ComparisonSideView
    created_at: AwareDatetime
    matched: BoundedList[MatchedView]
    unmatched: BoundedList[UnmatchedView]
    ambiguous: BoundedList[AmbiguousView]
    limitations: tuple[ComparisonLimitationView, ...]


def _comparison_output(
    session: ToolSession, result: ComparisonResult, requested: int
) -> ComparisonOutput:
    sides = {
        ComparisonSide.LEFT: result.left.snapshot_id,
        ComparisonSide.RIGHT: result.right.snapshot_id,
    }

    def ref(side: ComparisonSide, resource_id: str) -> SideRef:
        return SideRef(snapshot_id=sides[side], resource_id=resource_id)

    def qualification(item: Qualification) -> QualificationView:
        return QualificationView(
            kind=item.kind,
            side=item.side,
            service=item.service or (None if item.coverage is None else item.coverage.service),
            operation=None if item.coverage is None else item.coverage.operation,
            lifecycle_actions=()
            if item.lifecycle is None
            else tuple(action.value for action in item.lifecycle.actions),
            evidence=SideEvidence(
                snapshot_id=sides[item.side], evidence_ids=tuple(item.evidence_ids)
            ),
        )

    def difference(item: FieldDifference) -> DifferenceView:
        return DifferenceView(
            subject=item.subject,
            path=untrusted(item.path),
            kind=item.kind,
            left_value=json_text(item.left_value),
            right_value=json_text(item.right_value),
            left=SideEvidence(
                snapshot_id=sides[ComparisonSide.LEFT], evidence_ids=tuple(item.left_evidence_ids)
            ),
            right=SideEvidence(
                snapshot_id=sides[ComparisonSide.RIGHT],
                evidence_ids=tuple(item.right_evidence_ids),
            ),
            qualifications=tuple(qualification(q) for q in item.qualifications),
        )

    def matched(item: MatchedPair) -> MatchedView:
        return MatchedView(
            entry_id=item.id,
            basis=item.basis,
            left=ref(ComparisonSide.LEFT, item.left.resource_id),
            right=ref(ComparisonSide.RIGHT, item.right.resource_id),
            differences=tuple(difference(d) for d in item.differences),
            qualifications=tuple(qualification(q) for q in item.qualifications),
        )

    def unmatched(item: UnmatchedResource) -> UnmatchedView:
        return UnmatchedView(
            entry_id=item.id,
            side=item.side,
            resource=ref(item.side, item.resource.resource_id),
            qualifications=tuple(qualification(q) for q in item.qualifications),
        )

    def ambiguous(item: AmbiguousMatch) -> AmbiguousView:
        return AmbiguousView(
            entry_id=item.id,
            basis=item.basis,
            key=untrusted(item.key),
            left=tuple(ref(ComparisonSide.LEFT, r.resource_id) for r in item.left),
            right=tuple(ref(ComparisonSide.RIGHT, r.resource_id) for r in item.right),
        )

    matched_views = [matched(item) for item in result.matched]
    unmatched_views = [unmatched(item) for item in result.unmatched]
    ambiguous_views = [ambiguous(item) for item in result.ambiguous]

    def side(snapshot) -> ComparisonSideView:
        return ComparisonSideView(
            snapshot_id=snapshot.snapshot_id,
            source_kind=snapshot.source_kind,
            collection_started_at=snapshot.collection_started_at,
            collection_completed_at=snapshot.collection_completed_at,
        )

    def build(effective: int) -> ComparisonOutput:
        return ComparisonOutput(
            left=side(result.left),
            right=side(result.right),
            created_at=result.created_at,
            matched=capped(matched_views, requested, effective),
            unmatched=capped(unmatched_views, requested, effective),
            ambiguous=capped(ambiguous_views, requested, effective),
            limitations=tuple(
                ComparisonLimitationView(kind=item.kind, message=untrusted(item.message))
                for item in result.limitations
            ),
        )

    return fit(build, requested, session.limits.max_output_chars)


def _get_saved_comparison(
    session: ToolSession, arguments: SavedResultInput
) -> ComparisonOutput | ToolError:
    if not session.comparisons:
        return tool_error(ToolErrorCode.UNAVAILABLE)
    if arguments.number > len(session.comparisons):
        return tool_error(ToolErrorCode.NOT_FOUND)
    requested = min(arguments.limit, session.limits.result_limit)
    return _comparison_output(session, session.comparisons[arguments.number - 1], requested)


ANALYSIS_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        "list_analyzers",
        "List the five deterministic analyzers, their versions, and the parameters "
        "run_analyzer accepts for each.",
        EmptyInput,
        _list_analyzers,
    ),
    ToolDefinition(
        "run_analyzer",
        "Run one analyzer over the selected snapshot in memory and return its findings "
        "with citations, paths, and limitations. Nothing is saved.",
        RunAnalyzerInput,
        _run,
    ),
    ToolDefinition(
        "list_saved_results",
        "List the saved analysis results and comparisons the user selected for this session.",
        EmptyInput,
        _list_saved,
    ),
    ToolDefinition(
        "get_saved_analysis",
        "Read one user-selected saved analysis result by its number in list_saved_results.",
        SavedResultInput,
        _get_saved_analysis,
    ),
    ToolDefinition(
        "get_saved_comparison",
        "Read one user-selected saved comparison by its number in list_saved_results. "
        "Differences are recorded, not proof of change or drift.",
        SavedResultInput,
        _get_saved_comparison,
    ),
)
