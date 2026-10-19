"""Shared interpretation of recorded coverage for analyzer results."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from archdiscov.models.analysis.references import AnalysisLimitation, CoverageReference
from archdiscov.models.cloud.contracts import CloudOperationCoverage
from archdiscov.models.enums import AnalysisStatus, CoverageStatus
from archdiscov.models.resource_base import Resource

from .query_context import contextual_coverage

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


@dataclass(frozen=True)
class Qualification:
    status: AnalysisStatus
    limitations: list[AnalysisLimitation]


def coverage_limitation(record: CloudOperationCoverage) -> AnalysisLimitation:
    """Describe one recorded non-success outcome; never call for a successful one."""
    return AnalysisLimitation(
        code=f"coverage_{record.status}",
        message=f"Recorded {record.service} operation {record.operation} is {record.status}.",
        coverage=CoverageReference(
            scope=record.scope,
            location=record.location,
            service=record.service,
            operation=record.operation,
        ),
    )


def qualify_status(
    index: GraphIndex, resources: Iterable[Resource], status: AnalysisStatus
) -> Qualification:
    """Downgrade a conclusion using coverage recorded for the resources' contexts.

    Failed, incomplete, skipped, or unsupported operations add one limitation each
    (deduplicated across resources, in index order). A supported conclusion becomes
    partial; partial and insufficient-evidence conclusions are never upgraded, so
    an analyzer's own uncertainty is kept. Successful-zero collection is not a
    limitation, and absent coverage records are not interpreted here.
    """
    records, _ = contextual_coverage(index, resources)
    limitations = [
        coverage_limitation(record) for record in records if record.status != CoverageStatus.SUCCESS
    ]
    if limitations and status == AnalysisStatus.SUPPORTED:
        status = AnalysisStatus.PARTIAL
    return Qualification(status, limitations)
