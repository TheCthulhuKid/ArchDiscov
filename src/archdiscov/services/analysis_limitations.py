"""Limitation and unsupported-platform builders shared by analyzers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.graph.dependencies import DependencyEdge
from archdiscov.models.analysis.references import (
    UNSUPPORTED_PLATFORM_CODE,
    AnalysisLimitation,
    ResourceReference,
    UnresolvedTargetReference,
)
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.enums import AnalysisStatus

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

RESULT_LIMIT_CODE = "result_limit_reached"
EXTERNAL_CODE = "external_target"
UNRESOLVED_CODE = "unresolved_target"


def target_limitations(edge: DependencyEdge, subject: str) -> list[AnalysisLimitation]:
    """Nonlocal targets stay terminals: a limitation names them, no resource is invented."""
    if isinstance(edge.target, LocalTarget):
        return []
    if isinstance(edge.target, UnresolvedTarget):
        return [
            AnalysisLimitation(
                code=UNRESOLVED_CODE,
                message=f"The {subject} could not be resolved.",
                unresolved_target=UnresolvedTargetReference(
                    source_id=edge.source_id,
                    target=edge.target,
                    evidence_ids=list(edge.evidence_ids),
                ),
            )
        ]
    return [
        AnalysisLimitation(
            code=EXTERNAL_CODE,
            message=f"The {subject} is outside this snapshot.",
            evidence_ids=list(edge.evidence_ids),
        )
    ]


def unsupported_platform_finding(
    index: GraphIndex, subject: str, resources: list[ResourceReference] | None = None
) -> AnalysisFinding:
    """An actionable insufficient-evidence result for a cloud the analyzer does not cover."""
    platform = index.scope.platform
    return AnalysisFinding(
        snapshot_id=index.snapshot_id,
        summary=f"Analysis of {subject} is not available for {platform} snapshots.",
        status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
        resources=resources or [],
        limitations=[
            AnalysisLimitation(
                code=UNSUPPORTED_PLATFORM_CODE,
                message=(
                    f"Only AWS {subject} are supported; {platform} semantics are not reused. "
                    "Analyze an AWS snapshot instead."
                ),
            )
        ],
    )
