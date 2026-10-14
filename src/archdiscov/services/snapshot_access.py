from collections.abc import Iterator
from dataclasses import dataclass

from archdiscov.models.architecture import ArchitectureSnapshot, ExternalTarget, LocalTarget
from archdiscov.models.cloud_context import identity_location
from archdiscov.models.cloud_contracts import (
    CloudCoverage,
    CloudDependency,
    CloudEvidence,
    CloudOperationCoverage,
    CloudSource,
)
from archdiscov.models.cloud_scope import AwsScope
from archdiscov.models.cloud_snapshot import CloudSnapshot
from archdiscov.models.enums import AwsPartition, AwsRegion
from archdiscov.models.resource_base import Resource
from archdiscov.models.resource_tree import walk_resources

SupportedSnapshot = ArchitectureSnapshot | CloudSnapshot


def _resources_from_validated_snapshot(snapshot: SupportedSnapshot) -> Iterator[Resource]:
    """Borrow resources internally; callers must own the freshly validated snapshot."""
    for region in snapshot.regions.values():
        yield from walk_resources(region.resources)
    roots = (
        snapshot.global_resources
        if isinstance(snapshot, ArchitectureSnapshot)
        else snapshot.scope_resources
    )
    yield from walk_resources(roots)


def _partition(snapshot: ArchitectureSnapshot) -> AwsPartition:
    regions = [*snapshot.regions, *snapshot.coverage.regions]
    if any(str(region).startswith("cn-") for region in regions):
        return AwsPartition.AWS_CN
    if any(str(region).startswith("us-gov-") for region in regions):
        return AwsPartition.AWS_US_GOV
    for resource in walk_resources(snapshot.global_resources):
        physical = resource.identity.physical_id
        if physical and physical.startswith("arn:"):
            return AwsPartition(physical.split(":", 2)[1])
    return AwsPartition.AWS


def upgrade_snapshot(snapshot: ArchitectureSnapshot) -> CloudSnapshot:
    """Explicit detached conversion; does not migrate saved files or rewrite IDs."""
    legacy = ArchitectureSnapshot.model_validate(snapshot.model_dump(mode="python"))
    scope = AwsScope(account_id=legacy.account_id, partition=_partition(legacy))
    owners = {r.id: r for r in walk_resources(legacy.global_resources)}
    for region in legacy.regions.values():
        owners.update((r.id, r) for r in walk_resources(region.resources))
    operations = []
    for item in legacy.coverage.operations:
        data = item.model_dump(mode="python", exclude={"account_id", "region"})
        operations.append(
            CloudOperationCoverage(
                scope=scope,
                location=None if item.region == AwsRegion.GLOBAL else item.region,
                **data,
            )
        )
    evidence = {
        key: CloudEvidence(
            scope=scope,
            location=identity_location(owners[item.resource_id].identity),
            service=owners[item.resource_id].identity.service,
            **item.model_dump(mode="python"),
        )
        for key, item in legacy.evidence.items()
    }
    dependencies = []
    for region, items in legacy.dependencies_by_region.items():
        for item in items:
            target = item.target or LocalTarget(resource_id=item.target_id)
            if isinstance(target, ExternalTarget):
                target = {
                    "kind": "external",
                    "identifier": target.identifier,
                    "scope": {
                        "platform": "aws",
                        "account_id": target.account_id,
                        "partition": (
                            target.identifier.split(":", 2)[1]
                            if target.identifier.startswith("arn:")
                            else None
                        ),
                    },
                    "location": None if target.region == AwsRegion.GLOBAL else target.region,
                    "service": target.service,
                    "native_type": target.aws_resource_type,
                }
            dependencies.append(
                CloudDependency(
                    source_id=item.source_id,
                    target=target,
                    location=None if region == AwsRegion.GLOBAL else region,
                    relationship=item.relationship,
                    attributes=item.attributes,
                    evidence_ids=item.evidence_ids,
                )
            )
    return CloudSnapshot(
        snapshot_id=legacy.snapshot_id,
        scope=scope,
        name=legacy.name,
        metadata=legacy.metadata,
        collection_started_at=legacy.collection_started_at,
        collection_completed_at=legacy.collection_completed_at,
        source=CloudSource(platform="aws", provenance=legacy.source),
        regions={
            key.value: value.model_dump(mode="python") for key, value in legacy.regions.items()
        },
        scope_resources=legacy.global_resources,
        coverage=CloudCoverage(
            scope=scope,
            locations=legacy.coverage.regions,
            scope_wide=legacy.coverage.account_wide,
            operations=operations,
        ),
        evidence=evidence,
        dependencies=dependencies,
    )


@dataclass(frozen=True)
class SnapshotAccess:
    """Read-only contract adapter. Returned models are detached copies, not graph nodes."""

    _snapshot: CloudSnapshot

    @classmethod
    def from_snapshot(cls, snapshot: SupportedSnapshot) -> "SnapshotAccess":
        model = (
            upgrade_snapshot(snapshot)
            if isinstance(snapshot, ArchitectureSnapshot)
            else CloudSnapshot.model_validate(snapshot.model_dump(mode="python"))
        )
        return cls(model)

    def _borrow_snapshot(self) -> CloudSnapshot:
        """Internal read-only borrowing; never expose or mutate this owned detached model."""
        return self._snapshot

    @property
    def scope(self):
        return self._snapshot.scope.model_copy(deep=True)

    def roots(self) -> Iterator[tuple[str | None, Resource]]:
        for location, region in self._snapshot.regions.items():
            for resource in region.resources:
                yield str(location), resource.model_copy(deep=True)
        for resource in self._snapshot.scope_resources:
            yield None, resource.model_copy(deep=True)

    def resources(self) -> Iterator[Resource]:
        for _, root in self.roots():
            yield from walk_resources([root])

    def containment(self) -> Iterator[tuple[str, str]]:
        for resource in _resources_from_validated_snapshot(self._snapshot):
            for child in resource.children:
                yield resource.id, child.id

    @property
    def evidence(self):
        return {key: value.model_copy(deep=True) for key, value in self._snapshot.evidence.items()}

    @property
    def coverage(self):
        return self._snapshot.coverage.model_copy(deep=True)

    def dependencies(self):
        for item in self._snapshot.dependencies:
            yield item.model_copy(deep=True)
