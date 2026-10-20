"""Ordering, bounding, and group-level reporting shared by the network-path analyzers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from archdiscov.models.analysis.references import AnalysisLimitation, ResourceReference
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.enums import AnalysisStatus, NetworkDecision

from .analysis_limitations import RESULT_LIMIT_CODE, distinct_limitations
from .analysis_qualification import Qualification, qualify_status

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex
    from archdiscov.models.resource_base import Resource

DECISION_ORDER = [NetworkDecision.ALLOW, NetworkDecision.UNKNOWN, NetworkDecision.DENY]


def qualify_decision(
    index: GraphIndex, resources: Iterable[Resource], decision: NetworkDecision
) -> Qualification:
    """Allow and deny are supported conclusions, unknown is insufficient evidence, and
    limited coverage of the resources downgrades a supported one to partial."""
    status = (
        AnalysisStatus.INSUFFICIENT_EVIDENCE
        if decision == NetworkDecision.UNKNOWN
        else AnalysisStatus.SUPPORTED
    )
    return qualify_status(index, resources, status)


def bound_findings(
    index: GraphIndex,
    findings: Iterable[AnalysisFinding],
    *,
    order: Callable[[AnalysisFinding], object],
    limit: int,
    limitations: list[AnalysisLimitation],
    summary: str,
    truncation_message: str,
    resources: list[ResourceReference] | None = None,
) -> list[AnalysisFinding]:
    """Deduplicate and order the findings, keep `limit` of them, and add a group-level
    insufficient-evidence finding when there are limitations or the result was truncated."""
    ordered = sorted({finding.id: finding for finding in findings}.values(), key=order)
    kept = ordered[:limit]
    notes = list(limitations)
    if len(ordered) > len(kept):
        notes.append(AnalysisLimitation(code=RESULT_LIMIT_CODE, message=truncation_message))
    if notes:
        kept.append(
            AnalysisFinding(
                snapshot_id=index.snapshot_id,
                summary=summary,
                status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
                resources=resources or [],
                limitations=distinct_limitations(notes),
            )
        )
    return kept
