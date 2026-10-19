"""Bounded, cycle-safe structural path exploration through a graph index."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from archdiscov.models.enums import QueryErrorCode, QueryTruncationReason
from archdiscov.models.query.edges import QueryTraversalStep
from archdiscov.models.query.parameters import QueryPathLimits, QueryTraversalOptions
from archdiscov.models.query.requests import QueryPathsRequest
from archdiscov.models.query.results import QueryPath, QueryPathsResult, QueryTruncation

from .adjacency import adjacent_steps, far_end, is_terminal, step_evidence, terminal_limitation
from .queries import validate_request
from .query_context import context_limitations
from .query_errors import QueryError

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


class _Stop(Exception):
    """Internal signal that a result or work bound ended exploration."""

    def __init__(self, reason: QueryTruncationReason) -> None:
        self.reason = reason


@dataclass(frozen=True)
class _Partial:
    """Ordered steps from the start, and the stored resources already on the path."""

    start_id: str
    steps: tuple[QueryTraversalStep, ...] = ()
    visited: frozenset[str] = frozenset()

    @property
    def end_id(self) -> str:
        return far_end(self.steps[-1]) if self.steps else self.start_id

    @property
    def ends_at_terminal(self) -> bool:
        return bool(self.steps) and is_terminal(self.steps[-1])

    def extend(self, step: QueryTraversalStep) -> _Partial:
        return _Partial(self.start_id, (*self.steps, step), self.visited | {far_end(step)})


class _Explorer:
    """Breadth-first exploration under result and work budgets."""

    def __init__(
        self, index: GraphIndex, options: QueryTraversalOptions, limits: QueryPathLimits
    ) -> None:
        self._index, self._options, self._limits = index, options, limits
        self._work = 0

    def _extensions(self, partial: _Partial, *, charge: bool) -> Iterator[_Partial]:
        """Lazily yield selected, acyclic extensions, charging work per candidate examined."""
        for step, selected in adjacent_steps(self._index, partial.end_id, self._options):
            if charge:
                if self._work >= self._limits.work_limit:
                    raise _Stop(QueryTruncationReason.WORK_LIMIT)
                self._work += 1
            if selected and far_end(step) not in partial.visited:
                yield partial.extend(step)

    def explore(
        self, start_id: str, excluded: Iterable[str] = ()
    ) -> tuple[list[_Partial], set[QueryTruncationReason]]:
        # Excluded resources are seeded as visited, so no path can enter them.
        root = _Partial(start_id, visited=frozenset({start_id, *excluded}))
        found: list[_Partial] = []
        frontier = [root]
        try:
            for _ in range(self._limits.depth):
                level: list[_Partial] = []
                for partial in frontier:
                    if partial.ends_at_terminal:
                        continue
                    for path in self._extensions(partial, charge=True):
                        if len(found) >= self._limits.limit_result:
                            raise _Stop(QueryTruncationReason.RESULT_LIMIT)
                        found.append(path)
                        level.append(path)
                frontier = level
            # Only a fully explored final level can say that deeper structure remains.
            # The probe is uncharged: it is bounded by the paths already returned.
            if any(
                not partial.ends_at_terminal
                and next(self._extensions(partial, charge=False), None) is not None
                for partial in frontier
            ):
                return found, {QueryTruncationReason.DEPTH_LIMIT}
        except _Stop as stop:
            return found, {stop.reason}
        return found, set()


def query_paths(
    index: GraphIndex, request: QueryPathsRequest | Mapping[str, Any]
) -> QueryPathsResult:
    """Explore simple structural paths outward from one stored resource.

    Every nonempty prefix is a path, shortest first, then by step-order keys; the
    same order drives exploration, so bounded results are deterministic. A path
    never revisits a resource (this stops cycles), yet parallel edges and distinct
    routes remain separate paths. Nonlocal targets end a path with a limitation.
    One work unit examines one candidate edge, including filtered or cyclic ones.
    Paths are recorded structure, not reachability or effective access.
    """
    request = validate_request(QueryPathsRequest, request)
    start = index.get_resource(request.resource_id)
    if start is None:
        raise QueryError(QueryErrorCode.RESOURCE_NOT_FOUND)
    found, reasons = _Explorer(index, request.options, request.limits).explore(
        start.id, request.exclude_resource_ids
    )

    resources = {start.id: start}
    evidence = list(index.evidence_for_resource(start.id))
    paths = []
    for partial in found:
        evidence.extend(item for step in partial.steps for item in step_evidence(index, step))
        limitation = terminal_limitation(index, partial.steps[-1])
        if not partial.ends_at_terminal and partial.end_id not in resources:
            resources[partial.end_id] = index.get_resource(partial.end_id)
        paths.append(
            QueryPath(
                # Prefixes share step objects while exploring; give each path its own copies.
                steps=[step.model_copy(deep=True) for step in partial.steps],
                limitations=[limitation] if limitation is not None else [],
            )
        )
    _, limitations = context_limitations(
        index, (resources[key] for key in sorted(resources)), evidence
    )
    return QueryPathsResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource_id=start.id,
        paths=paths,
        limitations=limitations,
        truncation=QueryTruncation(reasons=[r for r in QueryTruncationReason if r in reasons]),
    )
