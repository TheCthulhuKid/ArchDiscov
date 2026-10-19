"""Security-group rule references from recorded reference edges, never rule re-parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.graph.dependencies import DependencyEdge
from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.references import (
    AnalysisLimitation,
)
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.architecture import LocalTarget
from archdiscov.models.enums import (
    AttachmentKind,
    ImpactKind,
    ImpactLabel,
    RouteTargetKind,
    RuleDirection,
    RuleReferenceSide,
)
from archdiscov.models.resource_base import Resource

from .analysis_limitations import target_limitations
from .security_group_findings import impact_finding

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

OUTGOING_SUMMARY = "A recorded rule of the selected security group references this group."
EXTERNAL_SUMMARY = (
    "A recorded rule of the selected security group references a group outside this snapshot."
)
INCOMING_SUMMARY = "A recorded rule of this security group references the selected group."
EXTERNAL_CODE = "external_target"
DIRECTION_UNRECORDED_CODE = "rule_direction_unrecorded"


def _direction(edge: DependencyEdge) -> RuleDirection | None:
    try:
        return RuleDirection(edge.attributes.get("direction"))
    except ValueError:
        return None


def _is_group_reference(edge: DependencyEdge) -> bool:
    """Prefix-list references share the edge kind but do not name a security group."""
    return edge.attributes.get("target_kind") != RouteTargetKind.PREFIX_LIST


def _direction_limitation(edge: DependencyEdge) -> AnalysisLimitation:
    return AnalysisLimitation(
        code=DIRECTION_UNRECORDED_CODE,
        message="A recorded group reference has no rule direction.",
        evidence_ids=list(edge.evidence_ids),
    )


def _referenced_group_id(edge: DependencyEdge, side: RuleReferenceSide) -> str | None:
    """The group at the far end of the rule, or None when it is not a stored resource."""
    if side == RuleReferenceSide.OTHER_REFERENCES_SELECTED:
        return edge.source_id
    return edge.target.resource_id if isinstance(edge.target, LocalTarget) else None


def _summary(side: RuleReferenceSide, other: Resource | None) -> str:
    if side == RuleReferenceSide.OTHER_REFERENCES_SELECTED:
        return INCOMING_SUMMARY
    return OUTGOING_SUMMARY if other is not None else EXTERNAL_SUMMARY


def rule_references(
    index: GraphIndex, group: Resource
) -> tuple[list[AnalysisFinding], list[AnalysisLimitation]]:
    """Observed rule references to and from the selected group, plus skipped-edge limits.

    Findings carry the original edge (protocol/port context as recorded) and rule
    direction; nothing evaluates whether traffic is allowed. A reference without a
    recorded direction cannot be labelled and becomes a group-level limitation.
    """
    findings: list[AnalysisFinding] = []
    skipped: list[AnalysisLimitation] = []
    kind = AttachmentKind.SECURITY_GROUP_REFERENCE
    outgoing = [
        (edge, RuleReferenceSide.SELECTED_REFERENCES_OTHER)
        for edge in index.outgoing_edges(group.id, attachment_kind=kind)
    ]
    incoming = [
        (edge, RuleReferenceSide.OTHER_REFERENCES_SELECTED)
        for edge in index.incoming_edges(group.id, attachment_kind=kind)
        if edge.source_id != group.id  # a self-reference is already listed as outgoing
    ]
    for edge, side in [*outgoing, *incoming]:
        if not _is_group_reference(edge):
            continue
        direction = _direction(edge)
        if direction is None:
            skipped.append(_direction_limitation(edge))
            continue
        other_id = _referenced_group_id(edge, side)
        other = index.get_resource(other_id) if other_id is not None else None
        impacted = [other] if other is not None else []
        findings.append(
            impact_finding(
                index,
                summary=_summary(side, other),
                impact=SecurityGroupImpact(
                    selected_group_id=group.id,
                    kind=ImpactKind.RULE_REFERENCE,
                    label=ImpactLabel.OBSERVED,
                    rule_direction=direction,
                    reference_side=side,
                ),
                edge=edge,
                resources=impacted,
                qualifying=[group, *impacted],
                limitations=target_limitations(edge, "referenced security group"),
            )
        )
    return findings, skipped
