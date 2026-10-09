from typing import Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models.enums import AwsRegion, AwsService
from .requests import ACCOUNT_WIDE_SERVICES, AwsAccountConfig, RegionScope, StrictModel


class ResolvedAwsScope(StrictModel):
    """Resolved inventory scope for snapshot metadata['collection_scope'], not completeness."""

    account_id: str = Field(pattern=r"^[0-9]{12}$")
    services: tuple[AwsService, ...]
    region_scope: RegionScope
    regions: tuple[AwsRegion, ...]

    @field_validator("services")
    @classmethod
    def validate_services(cls, services: tuple[AwsService, ...]) -> tuple[AwsService, ...]:
        return AwsAccountConfig.validate_services(services)

    @field_validator("regions")
    @classmethod
    def validate_regions(cls, regions: tuple[AwsRegion, ...]) -> tuple[AwsRegion, ...]:
        return AwsAccountConfig.validate_regions(regions)

    @model_validator(mode="after")
    def validate_resolved(self) -> Self:
        if not self.regions and not set(self.services) <= ACCOUNT_WIDE_SERVICES:
            raise ValueError("resolved regions are required for regional services")
        return self

    @property
    def account_wide(self) -> bool:
        return bool(set(self.services) & ACCOUNT_WIDE_SERVICES)

    @classmethod
    def from_account(cls, account: AwsAccountConfig, regions: tuple[AwsRegion, ...]) -> Self:
        if account.region_scope is RegionScope.EXPLICIT and regions != account.regions:
            raise ValueError("resolved regions must match explicit requested regions")
        return cls(
            account_id=account.expected_account_id,
            services=account.services,
            region_scope=account.region_scope,
            regions=regions,
        )

    def to_provenance(self) -> dict[str, JsonValue]:
        """Return credential-free JSON data for the existing snapshot metadata contract."""
        return self.model_dump(mode="json")
