from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from ..architecture import (
    AwsSource,
    CloudFormationSource,
    CollectionFailure,
    Evidence,
    LocalTarget,
    MockSource,
    SnapshotSource,
    TerraformPlanSource,
    TerraformStateSource,
    UnresolvedTarget,
    UnresolvedValue,
)
from ..coverage_key import CoverageKey
from ..coverage_outcomes import CloudCoverageContext, CoverageOutcome
from ..enums import (
    AwsPartition,
    CloudPlatform,
    DependencyRelationship,
    SnapshotSourceKind,
)
from ..resource_base import Model, NonEmptyString
from .scope import AzureScope, SupportedScope


class AzureFixtureSource(SnapshotSource):
    kind: Literal[SnapshotSourceKind.AZURE_FIXTURE] = SnapshotSourceKind.AZURE_FIXTURE
    label: NonEmptyString


CloudProvenance = Annotated[
    AwsSource
    | TerraformStateSource
    | TerraformPlanSource
    | CloudFormationSource
    | MockSource
    | AzureFixtureSource,
    Field(discriminator="kind"),
]


class CloudSource(Model):
    platform: CloudPlatform
    provenance: CloudProvenance

    @property
    def kind(self) -> SnapshotSourceKind:
        return self.provenance.kind

    @model_validator(mode="after")
    def validate_platform(self) -> Self:
        aws = isinstance(self.provenance, (AwsSource, CloudFormationSource))
        if aws and self.platform != CloudPlatform.AWS:
            raise ValueError("AWS provenance requires AWS platform")
        if isinstance(self.provenance, AzureFixtureSource) and self.platform != CloudPlatform.AZURE:
            raise ValueError("Azure fixture provenance requires Azure platform")
        return self


class CloudEvidence(Evidence):
    scope: SupportedScope
    location: NonEmptyString | None = None
    service: NonEmptyString


class CloudOperationCoverage(
    CoverageOutcome[CollectionFailure, UnresolvedValue], CloudCoverageContext
):
    pass


class CloudCoverage(Model):
    scope: SupportedScope
    locations: list[NonEmptyString]
    scope_wide: bool = Field(strict=True)
    operations: list[CloudOperationCoverage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if len(self.locations) != len(set(self.locations)):
            raise ValueError("coverage locations must be unique")
        if not self.locations and not self.scope_wide:
            raise ValueError("coverage requires locations or scope_wide collection")
        keys = [CoverageKey.from_record(item) for item in self.operations]
        if len(keys) != len(set(keys)):
            raise ValueError("coverage operation outcomes must be unique")
        for item in self.operations:
            if item.scope != self.scope:
                raise ValueError("operation coverage scope must match owning coverage")
            if item.location is None:
                if not self.scope_wide:
                    raise ValueError("scope-wide operation requires scope_wide coverage")
            elif item.location not in self.locations:
                raise ValueError("operation coverage location must be included in requested scope")
        return self


class AwsTargetScope(Model):
    """Target context can be unknown; never fill it from the source account."""

    platform: Literal[CloudPlatform.AWS] = CloudPlatform.AWS
    account_id: str | None = Field(default=None, pattern=r"^[0-9]{12}$")
    partition: AwsPartition | None = None


TargetScope = Annotated[AwsTargetScope | AzureScope, Field(discriminator="platform")]


class CloudExternalTarget(Model):
    kind: Literal["external"] = "external"
    scope: TargetScope
    identifier: NonEmptyString
    location: NonEmptyString | None = None
    service: NonEmptyString | None = None
    native_type: NonEmptyString | None = None


CloudTarget = Annotated[
    LocalTarget | CloudExternalTarget | UnresolvedTarget, Field(discriminator="kind")
]


class CloudDependency(Model):
    source_id: NonEmptyString
    target: CloudTarget
    location: NonEmptyString | None = None
    relationship: DependencyRelationship
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyString] = Field(min_length=1)
