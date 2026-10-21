"""Graph query tools over the step 25 query services and the session's `GraphIndex`.

Each tool calls the same service the `query` CLI calls, then returns allowlisted views
with explicit truncation. Every list is capped by the request and the session limits,
and lists shrink together, reporting `size_limit`, until the output fits the session's
size bound. Results are recorded structure and coverage, not reachability.
"""

from collections import Counter
from typing import Annotated
from uuid import UUID

from pydantic import Field

from archdiscov.models.cloud.scope import SupportedScope
from archdiscov.models.enums import (
    AttachmentKind,
    CoverageStatus,
    DependencyRelationship,
    QueryDirection,
    QueryEdgeSelection,
    QueryErrorCode,
    QueryTruncationReason,
    SnapshotSourceKind,
)
from archdiscov.models.query.results import QueryTruncation
from archdiscov.models.resource_base import Model, NonEmptyString
from archdiscov.services.neighbours import query_neighbours
from archdiscov.services.paths import query_paths
from archdiscov.services.queries import inspect_resource, search_resources
from archdiscov.services.query_errors import QueryError

from .contracts import (
    BoundedList,
    ToolError,
    ToolErrorCode,
    ToolOutput,
    Truncation,
    TruncationReason,
    tool_error,
)
from .registry import ToolDefinition, ToolInput
from .session import ToolSession
from .views import (
    AttributeView,
    CoverageView,
    EdgeView,
    EvidenceView,
    FactView,
    LimitationView,
    ResourceRef,
    ResourceView,
    attribute_views,
    capped,
    coverage_order,
    coverage_view,
    edge_view,
    evidence_view,
    fact_views,
    fit,
    limitation_view,
    resource_view,
)

# The services' own maximum, so tools can count what they drop.
_SERVICE_RESULT_LIMIT = 10_000
_PATH_WORK_LIMIT = 10_000

Limit = Annotated[int, Field(default=25, ge=1, le=100, strict=True)]
Text = Annotated[NonEmptyString, Field(max_length=1_024)]

_TRUNCATION_REASONS = {
    QueryTruncationReason.DEPTH_LIMIT: TruncationReason.DEPTH_LIMIT,
    QueryTruncationReason.RESULT_LIMIT: TruncationReason.RESULT_LIMIT,
    QueryTruncationReason.WORK_LIMIT: TruncationReason.WORK_LIMIT,
}


def _query_error(error: QueryError) -> ToolError:
    if error.code == QueryErrorCode.RESOURCE_NOT_FOUND:
        return tool_error(ToolErrorCode.NOT_FOUND)
    return tool_error(ToolErrorCode.INVALID_ARGUMENTS)


def _limit(session: ToolSession, requested: int) -> int:
    return min(requested, session.limits.result_limit)


class _SnapshotOutput(ToolOutput):
    snapshot_id: UUID
    scope: SupportedScope
    source_kind: SnapshotSourceKind


def _context(session: ToolSession) -> dict:
    index = session.index
    return {
        "snapshot_id": index.snapshot_id,
        "scope": index.scope,
        "source_kind": index.source_kind,
    }


# ---- search ------------------------------------------------------------------------


class SearchInput(ToolInput):
    """Exact filters combined with AND; omit a filter to leave it unrestricted."""

    resource_id: Text | None = None
    name: Text | None = None
    native_type: Text | None = None
    tag_key: Text | None = None
    tag_value: str | None = Field(default=None, max_length=1_024)
    physical_id: Text | None = Field(
        default=None, description="Requires native_type; matched in the snapshot's scope."
    )
    limit: Limit = 25


class SearchOutput(_SnapshotOutput):
    resources: BoundedList[ResourceView]
    limitations: BoundedList[LimitationView]


def _search(session: ToolSession, arguments: SearchInput) -> SearchOutput | ToolError:
    index = session.index
    request: dict = {"limit_result": _SERVICE_RESULT_LIMIT}
    for name in ("resource_id", "name", "native_type"):
        if getattr(arguments, name) is not None:
            request[name] = getattr(arguments, name)
    if arguments.tag_key is not None:
        request["tag"] = {"key": arguments.tag_key, "value": arguments.tag_value}
    elif arguments.tag_value is not None:
        return tool_error(ToolErrorCode.INVALID_ARGUMENTS, ["tag_value"])
    if arguments.physical_id is not None:
        request["physical_identifier"] = {
            "scope": index.scope.model_dump(mode="json"),
            "native_type": arguments.native_type or "",
            "physical_id": arguments.physical_id,
        }
    try:
        result = search_resources(index, request)
    except QueryError as error:
        return _query_error(error)
    requested = _limit(session, arguments.limit)
    limitations = [limitation_view(item) for item in result.limitations]

    def build(effective: int) -> SearchOutput:
        return SearchOutput(
            **_context(session),
            resources=capped(
                [resource_view(resource, effective) for resource in result.resources],
                requested,
                effective,
            ),
            limitations=capped(limitations, requested, effective),
        )

    return fit(build, requested, session.limits.max_output_chars)


# ---- inspect -----------------------------------------------------------------------


class ResourceInput(ToolInput):
    resource_id: Text


class InspectOutput(_SnapshotOutput):
    """One resource with its attributes, cited facts, evidence, coverage, and placement."""

    resource: ResourceView
    parent: ResourceRef | None
    children: BoundedList[ResourceRef]
    attributes: BoundedList[AttributeView]
    facts: BoundedList[FactView]
    evidence: BoundedList[EvidenceView]
    coverage: BoundedList[CoverageView]
    limitations: BoundedList[LimitationView]


def _inspect(session: ToolSession, arguments: ResourceInput) -> InspectOutput | ToolError:
    index = session.index
    try:
        result = inspect_resource(index, {"resource_id": arguments.resource_id})
    except QueryError as error:
        return _query_error(error)
    resource = result.resource
    parent = index.parent_id(resource.id)
    children = [ResourceRef(resource_id=child) for child in index.child_ids(resource.id)]
    attributes = attribute_views(resource)
    facts = fact_views(resource)
    evidence = [evidence_view(key, value) for key, value in sorted(result.evidence.items())]
    coverage = [coverage_view(record) for record in coverage_order(result.coverage)]
    limitations = [limitation_view(item) for item in result.limitations]
    requested = session.limits.result_limit

    def build(effective: int) -> InspectOutput:
        return InspectOutput(
            **_context(session),
            resource=resource_view(resource, effective),
            parent=None if parent is None else ResourceRef(resource_id=parent),
            children=capped(children, requested, effective),
            attributes=capped(attributes, requested, effective),
            facts=capped(facts, requested, effective),
            evidence=capped(evidence, requested, effective),
            coverage=capped(coverage, requested, effective),
            limitations=capped(limitations, requested, effective),
        )

    return fit(build, requested, session.limits.max_output_chars)


# ---- neighbours and paths ----------------------------------------------------------


class TraversalInput(ToolInput):
    resource_id: Text
    direction: QueryDirection = QueryDirection.OUTGOING
    edges: QueryEdgeSelection = Field(
        default=QueryEdgeSelection.DEPENDENCIES,
        description="containment is structural placement, not a dependency.",
    )
    relationships: tuple[DependencyRelationship, ...] = Field(default=(), max_length=20)
    attachment_kinds: tuple[AttachmentKind, ...] = Field(default=(), max_length=20)
    limit: Limit = 25


def _options(arguments: TraversalInput) -> dict:
    return {
        "direction": arguments.direction,
        "edge_selection": arguments.edges,
        "relationships": list(arguments.relationships),
        "attachment_kinds": list(arguments.attachment_kinds),
    }


class NeighboursOutput(_SnapshotOutput):
    resource_id: str
    steps: BoundedList[EdgeView]
    limitations: BoundedList[LimitationView]


def _neighbours(session: ToolSession, arguments: TraversalInput) -> NeighboursOutput | ToolError:
    index = session.index
    try:
        result = query_neighbours(
            index,
            {
                "resource_id": arguments.resource_id,
                "options": _options(arguments),
                "limit_result": _SERVICE_RESULT_LIMIT,
            },
        )
    except QueryError as error:
        return _query_error(error)
    requested = _limit(session, arguments.limit)
    steps = [edge_view(index, step) for step in result.steps]
    limitations = [limitation_view(item) for item in result.limitations]

    def build(effective: int) -> NeighboursOutput:
        return NeighboursOutput(
            **_context(session),
            resource_id=result.resource_id,
            steps=capped(steps, requested, effective),
            limitations=capped(limitations, requested, effective),
        )

    return fit(build, requested, session.limits.max_output_chars)


class PathsInput(TraversalInput):
    depth: int = Field(default=3, ge=1, le=5, strict=True, description="Steps per path.")


class PathView(Model):
    steps: tuple[EdgeView, ...]
    limitations: tuple[LimitationView, ...]


class PathsOutput(_SnapshotOutput):
    """Recorded structural paths from one resource; not reachability or access."""

    resource_id: str
    depth: int
    paths: BoundedList[PathView]
    limitations: BoundedList[LimitationView]


def _service_truncation(truncation: QueryTruncation, returned: int) -> Truncation | None:
    if not truncation.reasons:
        return None
    return Truncation(
        reasons=tuple(_TRUNCATION_REASONS[reason] for reason in truncation.reasons),
        returned=returned,
    )


def _paths(session: ToolSession, arguments: PathsInput) -> PathsOutput | ToolError:
    index = session.index
    requested = _limit(session, arguments.limit)
    depth = min(arguments.depth, session.limits.depth)
    try:
        result = query_paths(
            index,
            {
                "resource_id": arguments.resource_id,
                "options": _options(arguments),
                "limits": {
                    "depth": depth,
                    "limit_result": requested,
                    "work_limit": _PATH_WORK_LIMIT,
                },
            },
        )
    except QueryError as error:
        return _query_error(error)
    paths = [
        PathView(
            steps=tuple(edge_view(index, step) for step in path.steps),
            limitations=tuple(limitation_view(item) for item in path.limitations),
        )
        for path in result.paths
    ]
    limitations = [limitation_view(item) for item in result.limitations]

    def build(effective: int) -> PathsOutput:
        kept = paths[:effective]
        service = _service_truncation(result.truncation, len(kept))
        if effective < len(paths):
            reasons = set(service.reasons if service else ()) | {TruncationReason.SIZE_LIMIT}
            service = Truncation(
                reasons=tuple(reason for reason in TruncationReason if reason in reasons),
                returned=len(kept),
            )
        return PathsOutput(
            **_context(session),
            resource_id=result.resource_id,
            depth=depth,
            paths=BoundedList(items=tuple(kept), truncation=service),
            limitations=capped(limitations, requested, effective),
        )

    return fit(build, requested, session.limits.max_output_chars)


# ---- coverage ----------------------------------------------------------------------


class CoverageInput(ToolInput):
    """Recorded collection outcomes; a missing record is not proof of absence."""

    service: Text | None = None
    location: Text | None = None
    status: CoverageStatus | None = None
    limit: Limit = 25


class CoverageOutput(_SnapshotOutput):
    status_counts: dict[CoverageStatus, int]
    """Counts of every recorded outcome, before filters."""
    records: BoundedList[CoverageView]


def _coverage(session: ToolSession, arguments: CoverageInput) -> CoverageOutput:
    records = coverage_order(session.index.coverage_records())
    counts = Counter(record.status for record in records)
    selected = [
        coverage_view(record)
        for record in records
        if (arguments.service is None or record.service == arguments.service)
        and (arguments.location is None or record.location == arguments.location)
        and (arguments.status is None or record.status == arguments.status)
    ]
    requested = _limit(session, arguments.limit)

    def build(effective: int) -> CoverageOutput:
        return CoverageOutput(
            **_context(session),
            status_counts={status: counts[status] for status in CoverageStatus},
            records=capped(selected, requested, effective),
        )

    return fit(build, requested, session.limits.max_output_chars)


# ---- evidence ----------------------------------------------------------------------


class EvidenceInput(ToolInput):
    evidence_id: Text


class EvidenceOutput(_SnapshotOutput):
    """One evidence record, the facts it supports, and same-context coverage."""

    evidence: EvidenceView
    fact_paths: BoundedList[str]
    coverage: BoundedList[CoverageView]


def _evidence(session: ToolSession, arguments: EvidenceInput) -> EvidenceOutput | ToolError:
    index = session.index
    record = index.get_evidence(arguments.evidence_id)
    if record is None:
        return tool_error(ToolErrorCode.NOT_FOUND)
    owner = index.get_resource(record.resource_id)
    fact_paths = (
        []
        if owner is None
        else sorted(
            path for path, ids in owner.fact_evidence.items() if arguments.evidence_id in ids
        )
    )
    coverage = [
        coverage_view(item)
        for item in coverage_order(index.coverage_records())
        if item.scope == record.scope
        and item.location == record.location
        and item.service == record.service
    ]
    requested = session.limits.result_limit

    def build(effective: int) -> EvidenceOutput:
        return EvidenceOutput(
            **_context(session),
            evidence=evidence_view(arguments.evidence_id, record),
            fact_paths=capped(fact_paths, requested, effective),
            coverage=capped(coverage, requested, effective),
        )

    return fit(build, requested, session.limits.max_output_chars)


QUERY_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        "search_resources",
        "Search recorded resources in the selected snapshot with exact filters (AND). "
        "No match is not proof of absence; check the returned limitations.",
        SearchInput,
        _search,
    ),
    ToolDefinition(
        "inspect_resource",
        "Inspect one resource by stable ID: attributes, cited facts, evidence, coverage, "
        "and containment placement.",
        ResourceInput,
        _inspect,
    ),
    ToolDefinition(
        "get_neighbours",
        "List recorded edges adjacent to one resource. Containment is placement; "
        "dependencies are recorded configuration, not reachability.",
        TraversalInput,
        _neighbours,
    ),
    ToolDefinition(
        "find_paths",
        "Explore bounded structural paths from one resource, shortest first. "
        "A path is recorded structure, not proof of reachability or access.",
        PathsInput,
        _paths,
    ),
    ToolDefinition(
        "inspect_coverage",
        "List recorded collection outcomes for the snapshot, optionally filtered. "
        "Failed, skipped, unsupported, or incomplete outcomes limit any conclusion.",
        CoverageInput,
        _coverage,
    ),
    ToolDefinition(
        "get_evidence",
        "Retrieve one evidence record by ID with the facts it supports and the "
        "coverage of its collection context.",
        EvidenceInput,
        _evidence,
    ),
)
