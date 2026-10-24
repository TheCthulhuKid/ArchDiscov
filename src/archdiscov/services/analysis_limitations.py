"""Limitation and unsupported-platform builders shared by analyzers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from archdiscov.models.analysis.references import (
    UNSUPPORTED_PLATFORM_CODE,
    AnalysisLimitation,
    ResourceReference,
    UnresolvedTargetReference,
)
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.cloud.context import canonical_service_key
from archdiscov.models.edges import DependencyEdge
from archdiscov.models.enums import AnalysisStatus, AwsService, CoverageStatus
from archdiscov.models.network_path import NetworkBlocker

from .analysis_qualification import coverage_limitation

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


def blocker_limitation(blocker: NetworkBlocker) -> AnalysisLimitation:
    """A network blocker as a limitation: its code, message, coverage, and citations."""
    return AnalysisLimitation(
        code=blocker.code.value,
        message=blocker.message,
        coverage=blocker.coverage,
        evidence_ids=blocker.evidence_ids,
    )


def distinct_limitations(limitations: Iterable[AnalysisLimitation]) -> list[AnalysisLimitation]:
    """Drop identical limitations, keeping first occurrences in order."""
    return list({item.model_dump_json(): item for item in limitations}.values())


UNRECORDED_PREFIX = "coverage_unrecorded_"


def service_coverage_limitations(
    index: GraphIndex, services: Iterable[AwsService], region: str | None = None
) -> list[AnalysisLimitation]:
    """Limited or unrecorded collection for each named service.

    Only recorded operations are referenced; a service with no record gets a per-service
    code and no invented operation name. With a region, only records for that region or
    scope-wide ones count.
    """
    records = index.coverage_records()
    limitations: list[AnalysisLimitation] = []
    for service in services:
        matching = [
            record
            for record in records
            if canonical_service_key(index.scope.platform, record.service) == service
            and (region is None or record.location in (None, region))
        ]
        if not matching:
            where = f" for {region}" if region is not None else ""
            limitations.append(
                AnalysisLimitation(
                    code=f"{UNRECORDED_PREFIX}{service}",
                    message=f"No {service} collection is recorded{where}.",
                )
            )
        limitations.extend(
            coverage_limitation(record)
            for record in matching
            if record.status != CoverageStatus.SUCCESS
        )
    return limitations
