"""Evidence inspection over a freshly validated snapshot, without graph inference."""

from uuid import UUID

from archdiscov.models.architecture import (
    Coverage,
    Evidence,
    Model,
    SupportedResource,
    SupportedSource,
)

from .snapshots import SnapshotError, SnapshotInput, validate_snapshot


class EvidenceNotFoundError(SnapshotError):
    def __init__(self) -> None:
        super().__init__(
            "Evidence ID was not found in the selected snapshot.", code="evidence_not_found"
        )


class EvidenceInspection(Model):
    snapshot_id: UUID
    snapshot_source: SupportedSource
    evidence_id: str
    resource: SupportedResource
    evidence: Evidence
    fact_paths: list[str]
    coverage: list[Coverage]


def get_evidence(snapshot: SnapshotInput, evidence_id: str) -> EvidenceInspection:
    """Return a detached record, its owner, and same-region/service coverage context.

    Coverage is contextual, not an inferred dependency or a completeness guarantee.
    Uncited records remain inspectable; fact_paths lists only explicit fact citations.
    """
    validated = validate_snapshot(snapshot)
    if evidence_id not in validated.evidence:
        raise EvidenceNotFoundError()
    evidence = validated.evidence[evidence_id]
    pending = list(validated.global_resources)
    for region in validated.regions.values():
        pending.extend(region.resources)
    while pending:
        resource = pending.pop()
        if resource.id == evidence.resource_id:
            return EvidenceInspection(
                snapshot_id=validated.snapshot_id,
                snapshot_source=validated.source,
                evidence_id=evidence_id,
                resource=resource,
                evidence=evidence,
                fact_paths=sorted(
                    path for path, ids in resource.fact_evidence.items() if evidence_id in ids
                ),
                coverage=[
                    operation
                    for operation in validated.coverage.operations
                    if operation.region == resource.identity.region
                    and operation.service == resource.identity.service
                ],
            )
        pending.extend(resource.children)
    # Snapshot integrity validation guarantees every evidence owner is stored.
    raise AssertionError("Validated evidence owner was not found")
