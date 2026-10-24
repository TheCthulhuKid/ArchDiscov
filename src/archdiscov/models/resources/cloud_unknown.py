from typing import ClassVar, Literal, Self

from pydantic import model_validator

from ..cloud.identity import AzureResourceIdentity
from ..enums import CloudPlatform, ResourceType
from ..resource_base import NonEmptyString, Resource


class CloudUnknownResource(Resource):
    cloud_platform: ClassVar[None] = None
    resource_type: Literal[ResourceType.CLOUD_UNKNOWN] = ResourceType.CLOUD_UNKNOWN
    platform: CloudPlatform
    native_type: NonEmptyString

    @model_validator(mode="after")
    def validate_platform(self) -> Self:
        if self.identity.platform != self.platform:
            raise ValueError("unknown resource platform must match identity")
        if (
            isinstance(self.identity, AzureResourceIdentity)
            and self.native_type.casefold() != self.identity.native_type.casefold()
        ):
            raise ValueError("unknown resource native_type must match Azure identity")
        return self
