from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    IPvAnyNetwork,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .coverage_outcomes import AwsCoverageContext, CoverageOutcome
from .enums import (
    AwsRegion,
    AwsService,
    DependencyRelationship,
    EvidenceValueStatus,
    ResourceType,
    SnapshotSourceKind,
    SourceType,
)
from .resource_base import (  # noqa: F401  (re-exported for existing importers)
    EvidenceIds,
    FactPath,
    Model,
    NonEmptyString,
    Resource,
    ResourceIdentity,
    ResourceLifecycle,
)
from .resources.azure import AzureSubnet, AzureVirtualMachine, AzureVirtualNetwork
from .resources.cloud_unknown import CloudUnknownResource
from .resources.database import DbCluster, DbInstance, DbSubnetGroup
from .resources.ecs import EcsCluster, EcsService, EcsTask, EcsTaskDefinition
from .resources.functions import LambdaFunction
from .resources.iam import IamInstanceProfile, IamManagedPolicy, IamRole
from .resources.load_balancing import Listener, ListenerRule, LoadBalancer, TargetGroup
from .resources.network import (
    InternetGateway,
    NetworkAcl,
    NetworkInterface,
    RouteTable,
    SecurityGroup,
)
from .resources.queues import SqsQueue
from .sanitization import is_sensitive_path, sanitize_value
from .snapshot_integrity import (
    _cache_fact_representations,  # noqa: F401 (backwards-compatible helper imports)
    _validate_citations,
    _validate_fact_pointer,  # noqa: F401 (backwards-compatible helper imports)
    index_resources,
    validate_collection_window,
    validate_evidence,
    validate_resource_facts,
    validate_unresolved_resources,
)


class Ec2Instance(Resource):
    resource_type: Literal[ResourceType.EC2_INSTANCE] = ResourceType.EC2_INSTANCE
    instance_type: str | None = None


class UnknownResource(Resource):
    """Preserve resources until a dedicated model supports their AWS type."""

    resource_type: Literal[ResourceType.UNKNOWN] = ResourceType.UNKNOWN
    aws_resource_type: str


class Subnet(Resource):
    """Subnet-specific attributes; containment is inherited from Resource."""

    resource_type: Literal[ResourceType.SUBNET] = ResourceType.SUBNET
    availability_zone: str | None = None
    cidr_blocks: list[IPvAnyNetwork] | None = None


class Vpc(Resource):
    """VPC-specific attributes; containment is inherited from Resource."""

    resource_type: Literal[ResourceType.VPC] = ResourceType.VPC
    cidr_blocks: list[IPvAnyNetwork] | None = None


SupportedResource = Annotated[
    Vpc
    | Subnet
    | Ec2Instance
    | RouteTable
    | InternetGateway
    | NetworkAcl
    | SecurityGroup
    | NetworkInterface
    | IamRole
    | IamInstanceProfile
    | IamManagedPolicy
    | LoadBalancer
    | Listener
    | ListenerRule
    | TargetGroup
    | EcsCluster
    | EcsService
    | EcsTask
    | EcsTaskDefinition
    | LambdaFunction
    | DbInstance
    | DbCluster
    | DbSubnetGroup
    | SqsQueue
    | UnknownResource
    | AzureVirtualNetwork
    | AzureSubnet
    | AzureVirtualMachine
    | CloudUnknownResource,
    Field(discriminator="resource_type"),
]


# Resolve the recursive children annotation after all variants are defined.
for resource_model in (
    Resource,
    Vpc,
    Subnet,
    Ec2Instance,
    RouteTable,
    InternetGateway,
    NetworkAcl,
    SecurityGroup,
    NetworkInterface,
    IamRole,
    IamInstanceProfile,
    IamManagedPolicy,
    LoadBalancer,
    Listener,
    ListenerRule,
    TargetGroup,
    EcsCluster,
    EcsService,
    EcsTask,
    EcsTaskDefinition,
    LambdaFunction,
    DbInstance,
    DbCluster,
    DbSubnetGroup,
    SqsQueue,
    UnknownResource,
    AzureVirtualNetwork,
    AzureSubnet,
    AzureVirtualMachine,
    CloudUnknownResource,
):
    resource_model.model_rebuild()


class RegionArchitecture(Model):
    """Top-level regional resources; VPCs contain their own subtrees.

    The enclosing regions mapping supplies the AWS region name.
    """

    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    resources: list[SupportedResource] = Field(default_factory=list)


class AccountArchitecture(Model):
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    name: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    regions: dict[AwsRegion, RegionArchitecture] = Field(default_factory=dict)
    # IAM and other account-wide resources do not belong to an AWS region.
    global_resources: list[SupportedResource] = Field(default_factory=list)

    @field_validator("regions")
    @classmethod
    def reject_global_region(
        cls, regions: dict[AwsRegion, RegionArchitecture]
    ) -> dict[AwsRegion, RegionArchitecture]:
        if AwsRegion.GLOBAL in regions:
            raise ValueError("account-wide resources belong in global_resources, not regions")
        return regions


class LocalTarget(Model):
    """A target stored in this snapshot."""

    kind: Literal["local"] = "local"
    resource_id: NonEmptyString


class ExternalTarget(Model):
    """A known target outside this snapshot; it is never stored or invented as a resource."""

    kind: Literal["external"] = "external"
    identifier: NonEmptyString  # ARN, physical ID, or other source-provided reference
    account_id: str | None = Field(default=None, pattern=r"^[0-9]{12}$")
    region: AwsRegion | None = None
    service: AwsService | None = None
    aws_resource_type: NonEmptyString | None = None


class UnresolvedTarget(Model):
    """A reference that could not be resolved, with why and where it appeared."""

    kind: Literal["unresolved"] = "unresolved"
    reason: NonEmptyString
    source_path: NonEmptyString  # exact location of the reference within the source

    @model_validator(mode="after")
    def sanitize_location(self) -> Self:
        if is_sensitive_path(self.source_path):
            raise ValueError("unresolved target source_path must not name a sensitive field")
        return self


DependencyTarget = Annotated[
    LocalTarget | ExternalTarget | UnresolvedTarget, Field(discriminator="kind")
]


class Dependency(Model):
    """A relationship from a stored source resource.

    Schema 1 uses ``target_id`` (always local). Schema 2 uses a discriminated ``target``.
    """

    source_id: NonEmptyString
    target_id: NonEmptyString | None = None
    target: DependencyTarget | None = None
    relationship: DependencyRelationship
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyString] = Field(min_length=1)

    @model_validator(mode="after")
    def require_one_target_form(self) -> Self:
        if (self.target_id is None) == (self.target is None):
            raise ValueError("dependency requires exactly one of target_id or target")
        return self

    @model_serializer(mode="wrap")
    def omit_unused_target_form(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        data.pop("target" if self.target is None else "target_id", None)
        return data


class Evidence(Model):
    snapshot_id: UUID
    resource_id: NonEmptyString
    source: SourceType
    locator: NonEmptyString  # API operation or input filename
    configuration_path: NonEmptyString  # exact field within the source
    value: JsonValue = None
    value_status: EvidenceValueStatus = EvidenceValueStatus.PRESENT
    redacted_paths: list[Annotated[str, Field(pattern=r"^(?:/(?:[^~]|~[01])*)?$")]] = Field(
        default_factory=list
    )
    limitation: NonEmptyString | None = None

    @model_validator(mode="before")
    @classmethod
    def require_present_value(cls, data: Any) -> Any:
        if isinstance(data, dict):
            status = data.get("value_status", EvidenceValueStatus.PRESENT)
            present = status in (EvidenceValueStatus.PRESENT, EvidenceValueStatus.REDACTED)
            if present and "value" not in data:
                raise ValueError("present or redacted evidence requires an explicit value")
        return data

    @model_validator(mode="after")
    def sanitize_evidence_value(self) -> Self:
        if self.value_status in (EvidenceValueStatus.OMITTED, EvidenceValueStatus.UNRESOLVED):
            if self.value is not None or not self.limitation or self.redacted_paths:
                raise ValueError(
                    "unavailable evidence requires a limitation and no value/redactions"
                )
            return self
        self.value, paths = sanitize_value(
            self.value, sensitive=is_sensitive_path(self.configuration_path)
        )
        self.redacted_paths = sorted(set(self.redacted_paths + paths))
        if self.redacted_paths:
            self.value_status = EvidenceValueStatus.REDACTED
        elif self.value_status == EvidenceValueStatus.REDACTED:
            raise ValueError("redacted evidence requires redacted_paths")
        return self


class SnapshotSource(Model):
    exported_at: AwareDatetime | None = None


class AwsSource(SnapshotSource):
    """Live observations; profile and role describe collection, not credentials."""

    kind: Literal[SnapshotSourceKind.AWS] = SnapshotSourceKind.AWS
    profile: str | None = Field(default=None, min_length=1, pattern=r"\S")
    role_arn: str | None = Field(
        default=None,
        pattern=(
            r"^arn:aws(?:-[a-z0-9]+)*:iam::[0-9]{12}:role/"
            r"(?:[\x21-\x7E]{1,510}/)?[A-Za-z0-9_+=,.@-]{1,64}$"
        ),
    )


class TerraformSource(SnapshotSource):
    path: str = Field(min_length=1, pattern=r"\S")
    format_version: str = Field(min_length=1, pattern=r"\S")
    terraform_version: str | None = Field(default=None, min_length=1, pattern=r"\S")
    workspace: str | None = Field(default=None, min_length=1, pattern=r"\S")


class TerraformStateSource(TerraformSource):
    kind: Literal[SnapshotSourceKind.TERRAFORM_STATE] = SnapshotSourceKind.TERRAFORM_STATE
    lineage: str | None = Field(default=None, min_length=1, pattern=r"\S")
    serial: int | None = Field(default=None, ge=0, strict=True)


class TerraformPlanSource(TerraformSource):
    kind: Literal[SnapshotSourceKind.TERRAFORM_PLAN] = SnapshotSourceKind.TERRAFORM_PLAN


class CloudFormationSource(SnapshotSource):
    kind: Literal[SnapshotSourceKind.CLOUDFORMATION] = SnapshotSourceKind.CLOUDFORMATION
    path: str = Field(min_length=1, pattern=r"\S")
    stack_id: str | None = Field(default=None, min_length=1, pattern=r"\S")
    stack_name: str | None = Field(default=None, min_length=1, pattern=r"\S")
    template_format_version: str | None = Field(default=None, min_length=1, pattern=r"\S")


class MockSource(SnapshotSource):
    kind: Literal[SnapshotSourceKind.MOCK] = SnapshotSourceKind.MOCK
    label: str = Field(min_length=1, pattern=r"\S")


SupportedSource = Annotated[
    AwsSource | TerraformStateSource | TerraformPlanSource | CloudFormationSource | MockSource,
    Field(discriminator="kind"),
]


class CollectionFailure(Model):
    code: NonEmptyString
    message: NonEmptyString


class UnresolvedValue(Model):
    """A source fact unavailable to analysis; do not retain raw sensitive expressions."""

    resource_id: NonEmptyString | None = None
    configuration_path: NonEmptyString
    reason: NonEmptyString


class Coverage(CoverageOutcome[CollectionFailure, UnresolvedValue], AwsCoverageContext):
    """One aggregate outcome per account, region, service, and operation."""


class SnapshotCoverage(Model):
    """Requested collection scope, not a claim of successful or complete collection."""

    regions: list[AwsRegion]
    account_wide: bool = Field(strict=True)
    operations: list[Coverage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if AwsRegion.GLOBAL in self.regions:
            raise ValueError("account-wide coverage uses account_wide, not regions")
        if len(self.regions) != len(set(self.regions)):
            raise ValueError("coverage regions must be unique")
        if not self.regions and not self.account_wide:
            raise ValueError("coverage requires at least one region or account_wide collection")
        keys = [
            (item.account_id, item.region, item.service, item.operation) for item in self.operations
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("coverage operation outcomes must be unique")
        for item in self.operations:
            if item.region == AwsRegion.GLOBAL:
                if not self.account_wide:
                    raise ValueError("global operation coverage requires account_wide scope")
            elif item.region not in self.regions:
                raise ValueError("operation coverage region must be included in requested scope")
        return self


class ArchitectureSnapshot(AccountArchitecture):
    """One account's architecture, followed by dependencies and evidence."""

    schema_version: Literal["1", "2"]
    snapshot_id: UUID
    collection_started_at: AwareDatetime
    collection_completed_at: AwareDatetime
    source: SupportedSource
    coverage: SnapshotCoverage
    dependencies_by_region: dict[AwsRegion, list[Dependency]] = Field(default_factory=dict)
    evidence: dict[NonEmptyString, Evidence] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def reject_unsupported_version(cls, value: Any) -> Any:
        if value not in ("1", "2"):
            raise PydanticCustomError(
                "unsupported_schema_version",
                "Unsupported snapshot schema version {version}; supported versions: 1, 2",
                {"version": str(value)},
            )
        return value

    @model_validator(mode="after")
    def validate_collection_window(self) -> Self:
        validate_collection_window(self.collection_started_at, self.collection_completed_at, ())
        if set(self.regions) - set(self.coverage.regions):
            raise ValueError("snapshot regions must be included in coverage.regions")
        if self.global_resources and not self.coverage.account_wide:
            raise ValueError("global_resources require account_wide coverage")
        for item in self.coverage.operations:
            if item.account_id != self.account_id:
                raise ValueError("operation coverage account must match snapshot account")
            validate_collection_window(
                self.collection_started_at, self.collection_completed_at, (item,)
            )
        return self

    @model_validator(mode="after")
    def validate_snapshot_integrity(self) -> Self:
        resources: dict[str, Resource] = {}
        locations: dict[str, str] = {}
        for region, architecture in self.regions.items():
            _index_resources(
                architecture.resources,
                self.account_id,
                region,
                f"regions[{region}].resources",
                resources,
                locations,
            )
        _index_resources(
            self.global_resources,
            self.account_id,
            AwsRegion.GLOBAL,
            "global_resources",
            resources,
            locations,
        )
        validate_evidence(self.snapshot_id, resources, self.evidence)
        validate_resource_facts(resources, self.evidence)
        validate_unresolved_resources(self.coverage.operations, resources)
        for resource in resources.values():
            if resource.lifecycle is not None and not (
                self.schema_version == "2" and self.source.kind == SnapshotSourceKind.TERRAFORM_PLAN
            ):
                raise ValueError(
                    f"resource {resource.id!r}: lifecycle requires schema 2 and a Terraform plan"
                )
        for region, dependencies in self.dependencies_by_region.items():
            for position, dependency in enumerate(dependencies):
                location = f"dependencies_by_region[{region}][{position}]"
                if self.schema_version == "1":
                    if dependency.target is not None:
                        raise ValueError(f"{location}: schema 1 dependencies require target_id")
                    target_id: str | None = dependency.target_id
                else:
                    if dependency.target_id is not None:
                        raise ValueError(f"{location}: schema 2 dependencies require target")
                    local = dependency.target
                    target_id = local.resource_id if isinstance(local, LocalTarget) else None
                if dependency.source_id not in resources:
                    raise ValueError(f"{location}: unknown source_id {dependency.source_id!r}")
                if target_id is not None and target_id not in resources:
                    raise ValueError(f"{location}: unknown target_id {target_id!r}")
                if resources[dependency.source_id].identity.region != region:
                    raise ValueError(f"{location}: grouping must match source resource region")
                _validate_citations(dependency.evidence_ids, dependency.source_id, self.evidence)
        return self


def _index_resources(
    roots: list[SupportedResource],
    account_id: str,
    region: AwsRegion,
    location: str,
    resources: dict[str, Resource],
    locations: dict[str, str],
) -> None:
    def validate_placement(resource: Resource, current: str) -> None:
        if not isinstance(resource.identity, ResourceIdentity):
            raise ValueError(f"resource at {current}: legacy snapshots require AWS identities")
        if isinstance(
            resource, (CloudUnknownResource, AzureVirtualNetwork, AzureSubnet, AzureVirtualMachine)
        ):
            raise ValueError(f"resource at {current}: legacy snapshots require AWS resource models")
        if resource.identity.account_id != account_id:
            raise ValueError(
                f"resource {resource.id!r} at {current}: "
                f"account must match snapshot account {account_id}"
            )
        if resource.identity.region != region:
            raise ValueError(
                f"resource {resource.id!r} at {current}: "
                f"region must match enclosing region {region}"
            )

    index_resources(roots, location, resources, locations, validate_placement)
