"""Small evidence and coverage primitives shared by offline source importers."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from ..models.architecture import Coverage, Evidence, UnresolvedValue
from ..models.enums import AwsRegion, AwsService, CoverageStatus, EvidenceValueStatus, SourceType
from ..models.resource_base import Resource


def cite_source(
    evidence: dict[str, Evidence],
    resource: Resource,
    *,
    snapshot_id: UUID,
    source: SourceType,
    locator: str,
    path: str,
    value,
    reason: str | None = None,
    redacted: bool = False,
) -> list[str]:
    evidence_id = f"{source.value}:{len(evidence)}"
    evidence[evidence_id] = Evidence(
        snapshot_id=snapshot_id,
        resource_id=resource.id,
        source=source,
        locator=locator,
        configuration_path=path,
        value=value,
        value_status=EvidenceValueStatus.UNRESOLVED if reason else EvidenceValueStatus.PRESENT,
        limitation=reason,
        redacted_paths=[""] if redacted else [],
    )
    resource.evidence_ids.append(evidence_id)
    return [evidence_id]


def import_coverage(
    *,
    account_id: str,
    operation: str,
    counts: Mapping[tuple[AwsRegion, AwsService], int],
    limitations: Mapping[tuple[AwsRegion, AwsService], list[UnresolvedValue]],
    started: datetime,
    completed: datetime,
) -> list[Coverage]:
    return [
        Coverage(
            account_id=account_id,
            region=region,
            service=service,
            operation=operation,
            status=CoverageStatus.INCOMPLETE
            if limitations.get((region, service))
            else CoverageStatus.SUCCESS,
            result_count=count,
            unresolved_values=limitations.get((region, service), []),
            started_at=started,
            completed_at=completed,
        )
        for (region, service), count in sorted(counts.items())
    ]
