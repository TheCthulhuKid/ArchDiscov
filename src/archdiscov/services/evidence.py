"""Evidence inspection over a freshly validated snapshot, without graph inference."""

from uuid import UUID

from archdiscov.models.architecture import (
    Coverage,
    Evidence,
    Model,
    SupportedResource,
    SupportedSource,
)
from archdiscov.models.cloud.context import evidence_context_matches
from archdiscov.models.cloud.contracts import CloudEvidence, CloudOperationCoverage, CloudSource
from archdiscov.models.snapshot_access import _resources_from_validated_snapshot

from .snapshots import SnapshotError, SnapshotInput, validate_snapshot


class EvidenceNotFoundError(SnapshotError):
    def __init__(self) -> None:
        super().__init__(
            "Evidence ID was not found in the selected snapshot.", code="evidence_not_found"
        )


class EvidenceInspection(Model):
    snapshot_id: UUID
    snapshot_source: SupportedSource | CloudSource
    evidence_id: str
    resource: SupportedResource
    evidence: CloudEvidence | Evidence
    fact_paths: list[str]
    coverage: list[CloudOperationCoverage | Coverage]


def get_evidence(snapshot: SnapshotInput, evidence_id: str) -> EvidenceInspection:
    """Return a detached record, its owner, and same-region/service coverage context.

    Coverage is contextual, not an inferred dependency or a completeness guarantee.
    Uncited records remain inspectable; fact_paths lists only explicit fact citations.
    """
    validated = validate_snapshot(snapshot)
    if evidence_id not in validated.evidence:
        raise EvidenceNotFoundError()
    evidence = validated.evidence[evidence_id]
    for resource in _resources_from_validated_snapshot(validated):
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
                    if _matches_context(operation, resource, validated)
                ],
            )
    # Snapshot integrity validation guarantees every evidence owner is stored.
    raise AssertionError("Validated evidence owner was not found")


def _matches_context(operation, resource, snapshot) -> bool:
    if isinstance(operation, CloudOperationCoverage):
        return evidence_context_matches(
            resource.identity,
            snapshot.scope,
            operation.scope,
            operation.location,
            operation.service,
        )
    return (
        operation.account_id == snapshot.account_id
        and operation.region == resource.identity.region
        and operation.service == resource.identity.service
    )
