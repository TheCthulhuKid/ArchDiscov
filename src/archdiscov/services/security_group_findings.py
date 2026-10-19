"""Shared builders for security-group impact findings."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from archdiscov.graph.dependencies import DependencyEdge
from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.references import (
    AnalysisLimitation,
    DependencyPathStep,
    ResourceReference,
)
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.enums import AnalysisStatus
from archdiscov.models.resource_base import Resource

from .analysis_qualification import qualify_status

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


def edge_step(edge: DependencyEdge) -> DependencyPathStep:
    """The stored edge as a path step; citations and attributes stay exactly as recorded."""
    return DependencyPathStep(
        source_id=edge.source_id,
        target=edge.target,
        relationship=edge.relationship,
        evidence_ids=list(edge.evidence_ids),
        attributes=dict(edge.attributes),
    )


def resource_reference(index: GraphIndex, resource: Resource) -> ResourceReference:
    return ResourceReference(resource_id=resource.id, scope=index.scope)


def impact_finding(
    index: GraphIndex,
    *,
    summary: str,
    impact: SecurityGroupImpact,
    edge: DependencyEdge,
    resources: Iterable[Resource],
    qualifying: Iterable[Resource],
    limitations: Iterable[AnalysisLimitation] = (),
) -> AnalysisFinding:
    """One recorded edge as a cited impact finding, qualified by the resources' coverage.

    `resources` are the impacted resources named by the finding; `qualifying` are the
    resources whose recorded coverage can limit it. Both may be empty or differ.
    """
    qualified = qualify_status(index, qualifying, AnalysisStatus.SUPPORTED)
    return AnalysisFinding(
        snapshot_id=index.snapshot_id,
        summary=summary,
        status=qualified.status,
        evidence_ids=list(edge.evidence_ids),
        resources=[resource_reference(index, resource) for resource in resources],
        path=[edge_step(edge)],
        limitations=[*limitations, *qualified.limitations],
        impact=impact,
    )
