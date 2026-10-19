"""Direct security-group attachments from recorded membership edges."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.enums import AttachmentKind, ImpactKind, ImpactLabel
from archdiscov.models.resource_base import Resource

from .security_group_findings import impact_finding

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

SUMMARY = "Recorded configuration attaches this resource to the selected security group."


def direct_attachments(index: GraphIndex, group: Resource) -> list[AnalysisFinding]:
    """Observed attachments, one per recorded membership edge into the selected group.

    Membership is stored resource-to-group, so this uses incoming lookups and never
    reverses an edge. Containment (VPC or subnet placement) is never read: only an
    explicit membership edge is an attachment. Results are in edge order.
    """
    impact = SecurityGroupImpact(
        selected_group_id=group.id,
        kind=ImpactKind.DIRECT_ATTACHMENT,
        label=ImpactLabel.OBSERVED,
    )
    findings = []
    for edge in index.incoming_edges(
        group.id, attachment_kind=AttachmentKind.SECURITY_GROUP_MEMBERSHIP
    ):
        attached = index.get_resource(edge.source_id)
        findings.append(
            impact_finding(
                index,
                summary=SUMMARY,
                impact=impact,
                edge=edge,
                resources=[attached],
                qualifying=[attached],
            )
        )
    return findings
