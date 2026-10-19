"""Directional adjacency queries through an already-built graph index."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from archdiscov.graph.containment import ContainmentEdge
from archdiscov.graph.dependencies import DependencyEdge
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.enums import (
    QueryDirection,
    QueryEdgeSelection,
    QueryErrorCode,
    QueryTruncationReason,
)
from archdiscov.models.query.edges import (
    QueryContainmentEdge,
    QueryDependencyEdge,
    QueryTraversalStep,
)
from archdiscov.models.query.limitations import (
    QueryExternalTargetLimitation,
    QueryLimitation,
    QueryUnresolvedTargetLimitation,
)
from archdiscov.models.query.requests import QueryNeighboursRequest
from archdiscov.models.query.results import QueryNeighboursResult, QueryTruncation

from .queries import validate_request
from .query_context import (
    coverage_limitations,
    missing_coverage,
    no_coverage_limitation,
    resource_coverage,
)
from .query_errors import QueryError

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

_RANK = {QueryDirection.OUTGOING: 0, QueryDirection.INCOMING: 1}


def _step_key(step: QueryTraversalStep) -> tuple[str, str, str, str, int]:
    edge = step.edge
    stored = edge.edge
    edge_id = stored.id if isinstance(stored, DependencyEdge) else ""
    return edge.kind, stored.source_id, stored.target_id, edge_id, _RANK[step.direction]


def _terminal_limitation(index: GraphIndex, edge: DependencyEdge) -> QueryLimitation | None:
    """Describe a nonlocal target; it is never exposed as a stored resource."""
    if isinstance(edge.target, LocalTarget):
        return None
    terminal = index.get_terminal(edge.target_id)
    if terminal is None:
        return None
    if isinstance(terminal.target, UnresolvedTarget):
        return QueryUnresolvedTargetLimitation(
            terminal_id=terminal.id,
            edge_id=terminal.edge_id,
            target=terminal.target,
            message="Dependency target is unresolved; no resource identity is inferred.",
        )
    return QueryExternalTargetLimitation(
        terminal_id=terminal.id,
        edge_id=terminal.edge_id,
        target=terminal.target,
        message="Dependency target is external to this snapshot; it is not a stored resource.",
    )


def query_neighbours(
    index: GraphIndex, request: QueryNeighboursRequest | Mapping[str, Any]
) -> QueryNeighboursResult:
    """Return oriented adjacent edges for one stored resource.

    Reverse lookups only orient the stored edge; they assert no new relationship
    and keep the stored source's evidence. Containment is included only when
    selected and remains uncited. Results are recorded neighbours, not an
    exhaustive list; coverage context is reported as limitations.
    """
    request = validate_request(QueryNeighboursRequest, request)
    resource = index.get_resource(request.resource_id)
    if resource is None:
        raise QueryError(QueryErrorCode.RESOURCE_NOT_FOUND)
    options = request.options
    resource_id = resource.id
    outgoing = options.direction in (QueryDirection.OUTGOING, QueryDirection.BOTH)
    incoming = options.direction in (QueryDirection.INCOMING, QueryDirection.BOTH)
    steps: list[QueryTraversalStep] = []

    def dependency(edges: Iterable[DependencyEdge], direction: QueryDirection) -> None:
        relationships, kinds = set(options.relationships), set(options.attachment_kinds)
        steps.extend(
            QueryTraversalStep(edge=QueryDependencyEdge(edge=edge), direction=direction)
            for edge in edges
            if (not relationships or edge.relationship in relationships)
            and (not kinds or edge.attachment_kind in kinds)
        )

    def containment(edges: Iterable[ContainmentEdge], direction: QueryDirection) -> None:
        steps.extend(
            QueryTraversalStep(edge=QueryContainmentEdge(edge=edge), direction=direction)
            for edge in edges
        )

    if options.edge_selection != QueryEdgeSelection.CONTAINMENT:
        if outgoing:
            dependency(index.outgoing_edges(resource_id), QueryDirection.OUTGOING)
        if incoming:
            dependency(index.incoming_edges(resource_id), QueryDirection.INCOMING)

    if options.edge_selection != QueryEdgeSelection.DEPENDENCIES:
        if outgoing:
            containment(
                (ContainmentEdge(resource_id, child) for child in index.child_ids(resource_id)),
                QueryDirection.OUTGOING,
            )
        parent = index.parent_id(resource_id)
        if incoming and parent is not None:
            containment([ContainmentEdge(parent, resource_id)], QueryDirection.INCOMING)

    steps.sort(key=_step_key)
    returned = steps[: request.limit_result]

    scope = index.scope
    coverage = resource_coverage(index, resource)
    limitations = coverage_limitations(coverage)
    evidence = list(index.evidence_for_resource(resource_id))
    for step in returned:
        stored = step.edge.edge
        evidence.extend(
            record
            for record in (index.get_evidence(item) for item in stored.evidence_ids)
            if record is not None
        )
        if isinstance(stored, DependencyEdge):
            terminal = _terminal_limitation(index, stored)
            if terminal is not None:
                limitations.append(terminal)
    limitations.extend(missing_coverage(index, evidence))
    if not coverage:
        limitations.append(no_coverage_limitation(resource))
    return QueryNeighboursResult(
        snapshot_id=index.snapshot_id,
        scope=scope,
        resource_id=resource_id,
        steps=returned,
        limitations=limitations,
        truncation=QueryTruncation(
            reasons=[QueryTruncationReason.RESULT_LIMIT] if len(steps) > len(returned) else []
        ),
    )
