from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from .architecture import LocalTarget, SupportedResource
from .cloud_context import (
    evidence_context_matches,
    identity_location,
    validate_context,
    validate_identity_scope,
)
from .cloud_contracts import CloudCoverage, CloudDependency, CloudEvidence, CloudSource
from .cloud_scope import SupportedScope
from .enums import CloudPlatform, SnapshotSourceKind, SourceType
from .resource_base import Model, NonEmptyString
from .snapshot_integrity import (
    _validate_citations as validate_citations,
)
from .snapshot_integrity import (
    index_resources,
    validate_collection_window,
    validate_evidence,
    validate_resource_facts,
    validate_unresolved_resources,
)
from .validation_errors import validation_error


class CloudRegion(Model):
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    resources: list[SupportedResource] = Field(default_factory=list)


class CloudSnapshot(Model):
    schema_version: Literal["3"] = "3"
    snapshot_id: UUID
    scope: SupportedScope
    name: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    collection_started_at: AwareDatetime
    collection_completed_at: AwareDatetime
    source: CloudSource
    regions: dict[NonEmptyString, CloudRegion] = Field(default_factory=dict)
    scope_resources: list[SupportedResource] = Field(default_factory=list)
    coverage: CloudCoverage
    dependencies: list[CloudDependency] = Field(default_factory=list)
    evidence: dict[NonEmptyString, CloudEvidence] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if self.source.platform != self.scope.platform:
            raise validation_error(
                "cloud_snapshot_source_platform_mismatch",
                "source platform must match snapshot scope",
            )
        if self.coverage.scope != self.scope:
            raise validation_error(
                "cloud_snapshot_coverage_scope_mismatch", "coverage scope must match snapshot scope"
            )
        if set(self.regions) - set(self.coverage.locations):
            raise validation_error(
                "cloud_snapshot_locations_uncovered",
                "snapshot locations must be included in coverage",
            )
        if self.scope_resources and not self.coverage.scope_wide:
            raise validation_error(
                "cloud_snapshot_scope_coverage_required",
                "scope_resources require scope_wide coverage",
            )
        for location in self.coverage.locations:
            validate_context(self.scope, location, None)
        for item in self.coverage.operations:
            validate_context(item.scope, item.location, item.service)
        validate_collection_window(
            self.collection_started_at, self.collection_completed_at, self.coverage.operations
        )
        resources, locations = {}, {}

        def placement(location):
            def validate(resource, path):
                validate_identity_scope(resource.identity, self.scope)
                if identity_location(resource.identity) != location:
                    raise validation_error(
                        "cloud_snapshot_resource_location_mismatch",
                        f"resource at {path}: location must match enclosing location",
                    )

            return validate

        for location, region in self.regions.items():
            index_resources(
                region.resources,
                f"regions[{location}].resources",
                resources,
                locations,
                placement(location),
            )
        index_resources(
            self.scope_resources, "scope_resources", resources, locations, placement(None)
        )
        validate_evidence(self.snapshot_id, resources, self.evidence)
        validate_resource_facts(resources, self.evidence)
        validate_unresolved_resources(self.coverage.operations, resources)
        for record in self.evidence.values():
            owner = resources[record.resource_id]
            if not evidence_context_matches(
                owner.identity, self.scope, record.scope, record.location, record.service
            ):
                raise validation_error(
                    "cloud_snapshot_evidence_context_mismatch",
                    "evidence context must match owning resource",
                )
            if record.source in (SourceType.AWS, SourceType.CLOUDFORMATION):
                if self.scope.platform != CloudPlatform.AWS:
                    raise validation_error(
                        "cloud_snapshot_aws_evidence_scope_required",
                        "AWS evidence requires AWS scope",
                    )
            if record.source == SourceType.AZURE and self.scope.platform != CloudPlatform.AZURE:
                raise validation_error(
                    "cloud_snapshot_azure_evidence_scope_required",
                    "Azure evidence requires Azure scope",
                )
        for resource in resources.values():
            if resource.lifecycle is not None and not (
                self.scope.platform == CloudPlatform.AWS
                and self.source.kind == SnapshotSourceKind.TERRAFORM_PLAN
            ):
                raise validation_error(
                    "cloud_snapshot_lifecycle_source_invalid",
                    "lifecycle requires AWS Terraform plan source semantics",
                )
        for dependency in self.dependencies:
            if dependency.source_id not in resources:
                raise validation_error(
                    "cloud_snapshot_dependency_source_unknown", "dependency unknown source_id"
                )
            if isinstance(dependency.target, LocalTarget):
                if dependency.target.resource_id not in resources:
                    raise validation_error(
                        "cloud_snapshot_dependency_target_unknown", "dependency unknown target_id"
                    )
            else:
                target = dependency.target
                if hasattr(target, "scope"):
                    validate_context(target.scope, target.location, target.service)
            if dependency.location != identity_location(resources[dependency.source_id].identity):
                raise validation_error(
                    "cloud_snapshot_dependency_location_mismatch",
                    "dependency grouping must match source resource location",
                )
            validate_citations(dependency.evidence_ids, dependency.source_id, self.evidence)
        return self
