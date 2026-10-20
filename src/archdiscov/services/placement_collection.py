"""Shared accumulation of placement citations and limitations for database analysis."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.architecture import LocalTarget
from archdiscov.models.enums import AttachmentKind
from archdiscov.models.resource_base import Resource

from .analysis_limitations import target_limitations

if TYPE_CHECKING:
    from archdiscov.graph import DependencyEdge, GraphIndex


def limitation(code: str, message: str, evidence_ids: Iterable[str] = ()) -> AnalysisLimitation:
    return AnalysisLimitation(code=code, message=message, evidence_ids=list(evidence_ids))


class PlacementCollector:
    """Accumulates placement citations and limitations in deterministic order."""

    def __init__(self, index: GraphIndex) -> None:
        self.index = index
        self.evidence: set[str] = set()
        self.limitations: dict[tuple, AnalysisLimitation] = {}

    def note(self, item: AnalysisLimitation) -> None:
        key = (item.code, item.message, tuple(item.evidence_ids))
        self.limitations.setdefault(key, item)

    def sorted_limitations(self, ignore: Collection[str] = ()) -> tuple[AnalysisLimitation, ...]:
        """The noted limitations in deterministic order, without the ignored codes."""
        return tuple(
            self.limitations[key] for key in sorted(self.limitations) if key[0] not in ignore
        )

    def cite_edge(self, edge: DependencyEdge, noun: str) -> bool:
        """Cite the edge; True for a stored target, else its limitations are noted."""
        self.evidence.update(edge.evidence_ids)
        if isinstance(edge.target, LocalTarget):
            return True
        for item in target_limitations(edge, noun):
            self.note(item)
        return False

    def memberships(self, owners: list[Resource], kind: AttachmentKind, noun: str) -> set[str]:
        """Stored targets of the owners' membership edges; others become limitations."""
        found: set[str] = set()
        for owner in owners:
            for edge in self.index.outgoing_edges(owner.id, attachment_kind=kind):
                if self.cite_edge(edge, noun):
                    found.add(edge.target_id)
        return found
