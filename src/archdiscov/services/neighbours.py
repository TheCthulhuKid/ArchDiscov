"""Directional adjacency queries through an already-built graph index."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from archdiscov.models.enums import (
    QueryErrorCode,
    QueryTruncationReason,
)
from archdiscov.models.query.requests import QueryNeighboursRequest
from archdiscov.models.query.results import QueryNeighboursResult, QueryTruncation

from .adjacency import adjacent_steps, step_evidence, terminal_limitation
from .queries import validate_request
from .query_context import context_limitations
from .query_errors import QueryError

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


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
    steps = [step for step, selected in adjacent_steps(index, resource.id, options) if selected]
    returned = steps[: request.limit_result]

    evidence = list(index.evidence_for_resource(resource.id))
    for step in returned:
        evidence.extend(step_evidence(index, step))
    _, limitations = context_limitations(index, [resource], evidence)
    limitations.extend(
        limitation
        for limitation in (terminal_limitation(index, step) for step in returned)
        if limitation is not None
    )
    return QueryNeighboursResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource_id=resource.id,
        steps=returned,
        limitations=limitations,
        truncation=QueryTruncation(
            reasons=[QueryTruncationReason.RESULT_LIMIT] if len(steps) > len(returned) else []
        ),
    )
