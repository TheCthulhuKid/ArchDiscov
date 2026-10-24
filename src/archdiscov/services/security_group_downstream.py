"""Potential downstream impact: bounded dependency paths from impacted resources."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.parameters import SecurityGroupImpactParameters
from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.edges import DependencyEdge
from archdiscov.models.enums import (
    AnalysisStatus,
    ImpactKind,
    ImpactLabel,
    QueryDirection,
    QueryEdgeSelection,
    QueryTruncationReason,
)
from archdiscov.models.query.results import QueryPath

from .analysis_limitations import RESULT_LIMIT_CODE, target_limitations
from .analysis_qualification import qualify_status
from .paths import query_paths
from .security_group_findings import edge_step, resource_reference

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

SUMMARY = (
    "A recorded dependency chain from an impacted resource could carry downstream impact; "
    "this is a potential, not reachability or effective access."
)
# Total candidate-edge examinations shared by every trace of one analysis.
WORK_BUDGET = 100_000
MAX_TRACE_RESULTS = 10_000  # QueryLimitResult upper bound
_TRUNCATION = {
    QueryTruncationReason.DEPTH_LIMIT: (
        "depth_limit_reached",
        "Dependencies continue beyond the configured maximum depth.",
    ),
    QueryTruncationReason.RESULT_LIMIT: (
        RESULT_LIMIT_CODE,
        "More impact findings exist than the configured result limit.",
    ),
    QueryTruncationReason.WORK_LIMIT: (
        "work_limit_reached",
        "Tracing stopped at its fixed work budget before finishing.",
    ),
}


def truncation_limitations(reasons: Iterable[QueryTruncationReason]) -> list[AnalysisLimitation]:
    """One limitation per truncation reason, in a fixed order."""
    wanted = set(reasons)
    return [
        AnalysisLimitation(code=code, message=message)
        for reason, (code, message) in _TRUNCATION.items()
        if reason in wanted
    ]


def _edges(path: QueryPath) -> list[DependencyEdge]:
    return [step.edge.edge for step in path.steps]


def _finding(
    index: GraphIndex, group_id: str, edges: list[DependencyEdge], start_id: str
) -> AnalysisFinding:
    """One traced chain as a potential finding; only a local end names a resource."""
    last = edges[-1]
    local = index.get_resource(last.target_id)
    along = [index.get_resource(start_id)] + [
        resource
        for resource in (index.get_resource(edge.target_id) for edge in edges)
        if resource is not None
    ]
    qualified = qualify_status(index, along, AnalysisStatus.SUPPORTED)
    evidence = sorted({evidence_id for edge in edges for evidence_id in edge.evidence_ids})
    return AnalysisFinding(
        snapshot_id=index.snapshot_id,
        summary=SUMMARY,
        status=qualified.status,
        evidence_ids=evidence,
        resources=[resource_reference(index, local)] if local is not None else [],
        path=[edge_step(edge) for edge in edges],
        limitations=[
            *target_limitations(last, "referenced security group"),
            *qualified.limitations,
        ],
        impact=SecurityGroupImpact(
            selected_group_id=group_id,
            kind=ImpactKind.DOWNSTREAM_DEPENDENCY,
            label=ImpactLabel.POTENTIAL,
        ),
    )


def downstream_paths(
    index: GraphIndex,
    group_id: str,
    starts: Iterable[str],
    parameters: SecurityGroupImpactParameters,
    capacity: int,
) -> tuple[list[AnalysisFinding], set[QueryTruncationReason]]:
    """Trace outgoing dependencies from each start with the shared bounded traversal.

    Containment is never followed, and the selected group is excluded from traversal
    (re-entering it would only restate the observed attachment). Returns findings in
    start then path order, and the bounds that cut a trace short. Depth counts steps from
    the start. Only `capacity` findings can be kept, so tracing stops once one more than
    that exists, which is enough to report result truncation.
    """
    ordered = sorted(set(starts) - {group_id})
    if not ordered:
        return [], set()
    work_limit = max(1, WORK_BUDGET // len(ordered))
    findings: list[AnalysisFinding] = []
    reasons: set[QueryTruncationReason] = set()
    for start_id in ordered:
        if len(findings) > capacity:
            reasons.add(QueryTruncationReason.RESULT_LIMIT)
            break
        result = query_paths(
            index,
            {
                "resource_id": start_id,
                "options": {
                    "direction": QueryDirection.OUTGOING,
                    "edge_selection": QueryEdgeSelection.DEPENDENCIES,
                },
                "exclude_resource_ids": [group_id],
                "limits": {
                    "depth": parameters.max_depth,
                    "limit_result": min(capacity + 1, MAX_TRACE_RESULTS),
                    "work_limit": work_limit,
                },
            },
        )
        reasons.update(result.truncation.reasons)
        findings.extend(_finding(index, group_id, _edges(path), start_id) for path in result.paths)
    return findings, reasons
