from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from .enums import AwsPartition, CloudPlatform


def _canonical_uuid(value: str) -> str:
    return str(UUID(value))


UuidString = Annotated[
    str,
    Field(
        strict=True,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    ),
    AfterValidator(_canonical_uuid),
]


class AwsScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal[CloudPlatform.AWS] = CloudPlatform.AWS
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    partition: AwsPartition = AwsPartition.AWS


class AzureScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal[CloudPlatform.AZURE] = CloudPlatform.AZURE
    subscription_id: UuidString
    tenant_id: UuidString | None = None


SupportedScope = Annotated[AwsScope | AzureScope, Field(discriminator="platform")]
