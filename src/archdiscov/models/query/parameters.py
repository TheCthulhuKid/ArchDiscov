from typing import Annotated, Self

from pydantic import Field, model_validator

from archdiscov.models.enums import (
    AttachmentKind,
    DependencyRelationship,
    QueryDirection,
    QueryEdgeSelection,
)
from archdiscov.models.resource_base import Model

QueryDepth = Annotated[int, Field(ge=1, le=10)]
QueryLimitResult = Annotated[int, Field(ge=1, le=10_000)]
QueryWorkLimit = Annotated[int, Field(ge=1, le=1_000_000)]


class QueryTraversalOptions(Model):
    """Select traversal orientation and edge families without rewriting edges.

    Empty dependency filters impose no restriction. Values within each filter
    combine with OR; relationships and attachment kinds combine with AND.
    Attachment kinds filter attached_to dependencies, not tree placement.

    With BOTH edge families, filters affect dependencies only. Containment-only
    selection rejects dependency filters instead of silently ignoring them.
    Incoming traversal preserves stored direction and source-owned citations.
    """

    direction: QueryDirection = QueryDirection.OUTGOING
    edge_selection: QueryEdgeSelection = QueryEdgeSelection.DEPENDENCIES
    relationships: list[DependencyRelationship] = Field(default_factory=list)
    attachment_kinds: list[AttachmentKind] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dependency_filters(self) -> Self:
        if self.edge_selection == QueryEdgeSelection.CONTAINMENT and (
            self.relationships or self.attachment_kinds
        ):
            raise ValueError("Dependency filters cannot be used with containment-only selection")
        return self


class QueryPathLimits(Model):
    """Bound path depth, returned paths, and candidate-edge examinations.

    Depth is the number of steps in a path. One work unit examines one candidate
    edge at the current position, even if filters or cycle checks reject it.
    Re-examination along another path and parallel edges each consume work.

    Traversal must stop before exceeding a budget and report truncation only
    when work or results remain unexplored. These bounds do not limit index
    construction or guarantee an execution time; services enforce the budgets.
    """

    depth: QueryDepth = 3
    limit_result: QueryLimitResult = 100
    work_limit: QueryWorkLimit = 10_000
