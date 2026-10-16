import re
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    field_validator,
    model_validator,
)

from ..enums import AwsRegion, AwsService, CloudPlatform
from .namespace import AZURE_NAMESPACE_PATTERN
from .scope import AzureScope, UuidString


class ResourceIdentity(BaseModel):
    """Matching inputs independent of a particular snapshot or resource name."""

    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: AwsRegion
    service: AwsService
    physical_id: str | None = Field(default=None, min_length=1, pattern=r"\S")
    logical_address: str | None = Field(default=None, min_length=1, pattern=r"\S")

    @model_validator(mode="after")
    def require_identifier(self) -> Self:
        if self.physical_id is None and self.logical_address is None:
            raise ValueError("resource identity requires a physical_id or logical_address")
        return self

    @property
    def platform(self) -> CloudPlatform:
        return CloudPlatform.AWS

    @property
    def stable_id(self) -> str:
        kind = "physical" if self.physical_id is not None else "logical"
        identifier = self.physical_id if self.physical_id is not None else self.logical_address
        return f"{self.account_id}/{self.region}/{self.service}/{kind}/{quote(identifier, safe='')}"


_TYPE_SEGMENT = r"[A-Za-z][A-Za-z0-9_.-]*"


class AzureResourceIdentity(BaseModel):
    """Azure identity with case-insensitive ARM matching and preserved source spelling."""

    model_config = ConfigDict(extra="forbid")

    platform: Literal[CloudPlatform.AZURE] = CloudPlatform.AZURE
    subscription_id: UuidString
    tenant_id: UuidString | None = None
    resource_group: str | None = None
    location: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")
    service: str = Field(pattern=rf"^{AZURE_NAMESPACE_PATTERN}$")
    native_type: str = Field(pattern=rf"^{AZURE_NAMESPACE_PATTERN}(?:/{_TYPE_SEGMENT})+$")
    physical_id: str | None = None
    logical_address: str | None = None

    @field_validator("resource_group", "physical_id", "logical_address")
    @classmethod
    def validate_identifier_text(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("identity text must be nonempty without surrounding whitespace")
        return value

    @field_validator("resource_group")
    @classmethod
    def validate_resource_group(cls, value: str | None) -> str | None:
        if value is not None and any(char in value for char in "/\\?#%"):
            raise ValueError("resource_group must be a single ARM path segment")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.physical_id is None and self.logical_address is None:
            raise ValueError("resource identity requires a physical_id or logical_address")
        if self.native_type.split("/", 1)[0].lower() != self.service.lower():
            raise ValueError("native_type namespace must match service")
        if self.physical_id is not None:
            self._validate_arm_id()
        return self

    def _validate_arm_id(self) -> None:
        segments = self.physical_id.split("/")
        if (
            len(segments) < 7
            or segments[0] != ""
            or segments[1].lower() != "subscriptions"
            or any(not segment or segment != segment.strip() for segment in segments[1:])
            or any(char in self.physical_id for char in "\\?#%")
        ):
            raise ValueError("physical_id must be a subscription-scoped ARM resource ID")
        subscription = AzureScope(subscription_id=segments[2]).subscription_id
        if subscription != self.subscription_id:
            raise ValueError("ARM subscription must match identity subscription_id")
        provider_index = 3
        group = None
        if segments[3].lower() == "resourcegroups":
            group = segments[4]
            provider_index = 5
        if (group.lower() if group is not None else None) != (
            self.resource_group.lower() if self.resource_group is not None else None
        ):
            raise ValueError("ARM resource group must match identity resource_group")
        if len(segments) < provider_index + 4 or segments[provider_index].lower() != "providers":
            raise ValueError("ARM resource ID requires providers namespace and type/name pairs")
        namespace = segments[provider_index + 1]
        pairs = segments[provider_index + 2 :]
        if len(pairs) % 2 or any(
            re.fullmatch(_TYPE_SEGMENT, segment) is None for segment in pairs[::2]
        ):
            raise ValueError("ARM resource hierarchy must alternate resource types and names")
        native_type = "/".join([namespace, *pairs[::2]])
        if native_type.lower() != self.native_type.lower():
            raise ValueError("ARM resource type must match identity native_type")

    @property
    def scope(self) -> AzureScope:
        return AzureScope(subscription_id=self.subscription_id, tenant_id=self.tenant_id)

    @property
    def canonical_arm_id(self) -> str | None:
        return self.physical_id.lower() if self.physical_id is not None else None

    @property
    def stable_id(self) -> str:
        if self.physical_id is not None:
            return f"azure/{self.subscription_id}/physical/{quote(self.canonical_arm_id, safe='')}"
        parts = (
            self.native_type.lower(),
            self.resource_group.lower() if self.resource_group is not None else "",
            self.location.lower() if self.location is not None else "",
            self.service.lower(),
            self.logical_address,
        )
        return f"azure/{self.subscription_id}/logical/" + "/".join(
            quote(part, safe="") for part in parts
        )


def _identity_platform(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("platform", CloudPlatform.AWS)
    return getattr(value, "platform", None)


SupportedIdentity = Annotated[
    Annotated[ResourceIdentity, Tag("aws")] | Annotated[AzureResourceIdentity, Tag("azure")],
    Discriminator(_identity_platform),
]
_IDENTITY_ADAPTER = TypeAdapter(SupportedIdentity)


def parse_identity(data: Any) -> ResourceIdentity | AzureResourceIdentity:
    return _IDENTITY_ADAPTER.validate_python(data)
