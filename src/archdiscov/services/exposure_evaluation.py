"""The evaluated candidate exposure of one entry point on one port range."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from archdiscov.models.analysis.exposure import ExposureElementDecision
from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.enums import (
    ExposureElement,
    ExposureEntryKind,
    ExposureProtocol,
    NetworkDecision,
    TargetHealthState,
)
from archdiscov.models.network_path import NetworkResult, compose_decisions
from archdiscov.models.resource_base import Resource

from .element_results import blocker_limitations, element_decisions, result_evidence


@dataclass(frozen=True)
class ExposureEvaluation:
    """One entry point's candidate exposure on a port range, with citations and blockers."""

    kind: ExposureEntryKind
    entry: Resource
    protocol: ExposureProtocol
    start: int
    end: int
    decision: NetworkDecision
    elements: tuple[ExposureElementDecision, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[AnalysisLimitation, ...]
    targets: tuple[Resource, ...] = ()
    observed_target_health: tuple[TargetHealthState, ...] = ()

    @property
    def shape(self) -> tuple:
        """What must be equal for adjacent port ranges to be reported as one."""
        return (self.kind, self.protocol, self.elements, self.targets, self.observed_target_health)


def evaluate_elements(
    kind: ExposureEntryKind,
    entry: Resource,
    protocol: ExposureProtocol,
    ports: tuple[int, int],
    results: Mapping[ExposureElement, NetworkResult],
    *,
    evidence: set[str] | frozenset[str] = frozenset(),
    limitations: tuple[AnalysisLimitation, ...] = (),
    targets: tuple[Resource, ...] = (),
    health: tuple[TargetHealthState, ...] = (),
) -> ExposureEvaluation:
    """Compose element results into one evaluation; results are keyed by element."""
    present = list(results.values())
    return ExposureEvaluation(
        kind=kind,
        entry=entry,
        protocol=protocol,
        start=ports[0],
        end=ports[1],
        decision=compose_decisions(result.decision for result in present),
        elements=element_decisions(ExposureElementDecision, results, list(ExposureElement)),
        evidence_ids=tuple(sorted({*evidence, *result_evidence(present)})),
        limitations=(*limitations, *blocker_limitations(present)),
        targets=targets,
        observed_target_health=health,
    )


def merge_adjacent(evaluations: list[ExposureEvaluation]) -> list[ExposureEvaluation]:
    """Join consecutive port ranges that were decided identically, keeping all citations."""
    merged: list[ExposureEvaluation] = []
    for item in evaluations:
        last = merged[-1] if merged else None
        if last and last.shape == item.shape and last.end + 1 == item.start:
            limitations = list(
                {x.model_dump_json(): x for x in (*last.limitations, *item.limitations)}.values()
            )
            merged[-1] = replace(
                last,
                end=item.end,
                evidence_ids=tuple(sorted({*last.evidence_ids, *item.evidence_ids})),
                limitations=tuple(limitations),
            )
        else:
            merged.append(item)
    return merged
