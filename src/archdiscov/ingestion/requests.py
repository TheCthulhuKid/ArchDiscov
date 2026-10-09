from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from ..models.enums import AwsRegion, AwsService

ACCOUNT_WIDE_SERVICES = frozenset({AwsService.IAM})
MAX_CONCURRENCY_LIMIT = 16
COLLECTABLE_SERVICES = frozenset(
    {
        AwsService.EC2,
        AwsService.ELASTIC_LOAD_BALANCING,
        AwsService.ECS,
        AwsService.LAMBDA,
        AwsService.RDS,
        AwsService.SQS,
        AwsService.IAM,
    }
)

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class RegionScope(StrEnum):
    EXPLICIT = "explicit"
    ENABLED = "enabled"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRequest(StrictModel):
    """Base for typed requests that select an adapter."""

    kind: str


class AwsAccountConfig(StrictModel):
    """Explicit configuration for one account; one snapshot is produced per account."""

    profile: NonBlank
    expected_account_id: str = Field(pattern=r"^[0-9]{12}$")
    regions: tuple[AwsRegion, ...] = ()
    services: tuple[AwsService, ...] = (AwsService.EC2,)
    all_services: bool = Field(default=False, strict=True)
    region_scope: RegionScope = RegionScope.EXPLICIT
    bootstrap_region: AwsRegion | None = None
    role_arn: str | None = Field(default=None, pattern=r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/\S+$")
    external_id: NonBlank | None = Field(default=None, repr=False)
    session_name: NonBlank = "archdiscov"
    max_concurrency: int = Field(default=4, ge=1, le=MAX_CONCURRENCY_LIMIT, strict=True)
    max_in_flight_calls: int = Field(default=4, ge=1, le=MAX_CONCURRENCY_LIMIT, strict=True)
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=300, strict=True)
    read_timeout_seconds: float = Field(default=30.0, gt=0, le=300, strict=True)
    max_attempts: int = Field(default=3, ge=1, le=10, strict=True)

    @model_validator(mode="before")
    @classmethod
    def select_scope(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        selected = dict(data)
        if selected.get("all_services") is True:
            if "services" in selected:
                raise ValueError("all_services cannot be combined with explicit services")
            selected["services"] = tuple(sorted(COLLECTABLE_SERVICES))
            if not selected.get("regions"):
                selected.setdefault("region_scope", RegionScope.ENABLED)
        return selected

    @model_serializer(mode="wrap")
    def serialize_selection(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if self.all_services:
            data.pop("services", None)
        return data

    @field_validator("regions")
    @classmethod
    def validate_regions(cls, regions: tuple[AwsRegion, ...]) -> tuple[AwsRegion, ...]:
        if AwsRegion.GLOBAL in regions:
            raise ValueError("regions must name concrete regions; 'global' is not requestable")
        if len(regions) != len(set(regions)):
            raise ValueError("regions must be unique")
        return regions

    @field_validator("services")
    @classmethod
    def validate_services(cls, services: tuple[AwsService, ...]) -> tuple[AwsService, ...]:
        if not services:
            raise ValueError("at least one service is required")
        if len(services) != len(set(services)):
            raise ValueError("services must be unique")
        unsupported = [s.value for s in services if s not in COLLECTABLE_SERVICES]
        if unsupported:
            raise ValueError(f"services are not collectable: {', '.join(sorted(unsupported))}")
        return services

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.bootstrap_region is AwsRegion.GLOBAL:
            raise ValueError("bootstrap_region must name a concrete region")
        if self.region_scope is RegionScope.ENABLED and self.regions:
            raise ValueError("enabled region scope cannot include explicit regions")
        if (
            self.region_scope is RegionScope.EXPLICIT
            and not self.regions
            and not set(self.services) <= ACCOUNT_WIDE_SERVICES
        ):
            raise ValueError("regions are required unless only account-wide services are selected")
        if self.external_id is not None and self.role_arn is None:
            raise ValueError("external_id requires role_arn")
        return self


class AwsCollectionRequest(SourceRequest):
    """Collect each configured account into its own snapshot."""

    kind: Literal["aws"] = "aws"
    accounts: tuple[AwsAccountConfig, ...] = Field(min_length=1)
    max_in_flight_calls: int = Field(default=16, ge=1, le=64, strict=True)
    max_active_accounts: int = Field(default=2, ge=1, le=16, strict=True)
    max_collector_jobs: int = Field(default=8, ge=1, le=64, strict=True)

    @field_validator("accounts")
    @classmethod
    def validate_accounts(
        cls, accounts: tuple[AwsAccountConfig, ...]
    ) -> tuple[AwsAccountConfig, ...]:
        ids = [a.expected_account_id for a in accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("expected_account_id values must be unique")
        return accounts
