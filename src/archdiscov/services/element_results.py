"""Summaries of per-element network results shared by the path-composing analyzers."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TypeVar

from archdiscov.models.analysis.element_decisions import ElementDecision
from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.network_path import NetworkBlocker, NetworkResult

from .analysis_limitations import blocker_limitation

D = TypeVar("D", bound=ElementDecision)


def element_decisions[E: StrEnum](
    decision_type: type[D], results: Mapping[E, NetworkResult], order: list[E]
) -> tuple[D, ...]:
    """One decision per present element, in canonical order, with sorted blocker codes."""
    return tuple(
        decision_type(
            element=element,
            decision=results[element].decision,
            blocker_codes=sorted({b.code for b in results[element].blockers}),
        )
        for element in order
        if element in results
    )


def distinct_blockers(results: list[NetworkResult]) -> list[NetworkBlocker]:
    unique = {(b.code, b.message, tuple(b.evidence_ids)): b for r in results for b in r.blockers}
    return list(unique.values())


def result_evidence(results: list[NetworkResult]) -> set[str]:
    """Every citation of the results: allow evidence and the evidence of their blockers."""
    return {
        *(e for r in results for e in r.evidence_ids),
        *(e for b in distinct_blockers(results) for e in b.evidence_ids),
    }


def blocker_limitations(results: list[NetworkResult]) -> list[AnalysisLimitation]:
    return [blocker_limitation(b) for b in distinct_blockers(results)]
