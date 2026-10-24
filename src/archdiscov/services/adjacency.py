"""Oriented adjacency shared by neighbour and path queries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.cloud.contracts import CloudEvidence
from archdiscov.models.edges import ContainmentEdge, DependencyEdge
from archdiscov.models.enums import QueryDirection, QueryEdgeSelection
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
from archdiscov.models.query.parameters import QueryTraversalOptions

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

_RANK = {QueryDirection.OUTGOING: 0, QueryDirection.INCOMING: 1}


def step_key(step: QueryTraversalStep) -> tuple[str, str, str, str, int]:
    edge = step.edge
    stored = edge.edge
    edge_id = stored.id if isinstance(stored, DependencyEdge) else ""
    return edge.kind, stored.source_id, stored.target_id, edge_id, _RANK[step.direction]


def far_end(step: QueryTraversalStep) -> str:
    """The node a step arrives at: the stored target going out, the source coming in."""
    stored = step.edge.edge
    return stored.target_id if step.direction == QueryDirection.OUTGOING else stored.source_id


def is_terminal(step: QueryTraversalStep) -> bool:
    """Only an outgoing nonlocal dependency ends at a terminal, never a stored resource."""
    stored = step.edge.edge
    return (
        step.direction == QueryDirection.OUTGOING
        and isinstance(stored, DependencyEdge)
        and not isinstance(stored.target, LocalTarget)
    )


def adjacent_steps(
    index: GraphIndex, resource_id: str, options: QueryTraversalOptions
) -> list[tuple[QueryTraversalStep, bool]]:
    """Every candidate step in the selected families and directions, in step order.

    The flag says whether the dependency filters select the candidate; containment
    is always selected. Unselected candidates are kept so callers can account for
    examining them.
    """
    outgoing = options.direction in (QueryDirection.OUTGOING, QueryDirection.BOTH)
    incoming = options.direction in (QueryDirection.INCOMING, QueryDirection.BOTH)
    relationships, kinds = set(options.relationships), set(options.attachment_kinds)
    steps: list[tuple[QueryTraversalStep, bool]] = []

    def dependency(edges: Iterable[DependencyEdge], direction: QueryDirection) -> None:
        steps.extend(
            (
                QueryTraversalStep(edge=QueryDependencyEdge(edge=edge), direction=direction),
                (not relationships or edge.relationship in relationships)
                and (not kinds or edge.attachment_kind in kinds),
            )
            for edge in edges
        )

    def containment(edges: Iterable[ContainmentEdge], direction: QueryDirection) -> None:
        steps.extend(
            (QueryTraversalStep(edge=QueryContainmentEdge(edge=edge), direction=direction), True)
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
    steps.sort(key=lambda item: step_key(item[0]))
    return steps


def terminal_limitation(index: GraphIndex, step: QueryTraversalStep) -> QueryLimitation | None:
    """Describe a nonlocal target; it is never exposed as a stored resource."""
    if not is_terminal(step):
        return None
    edge = step.edge.edge
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


def step_evidence(index: GraphIndex, step: QueryTraversalStep) -> list[CloudEvidence]:
    return [
        record
        for record in (index.get_evidence(item) for item in step.edge.edge.evidence_ids)
        if record is not None
    ]
