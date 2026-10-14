from typing import ClassVar, Literal, Self

from pydantic import IPvAnyNetwork, model_validator

from .cloud_identity import AzureResourceIdentity
from .enums import CloudPlatform, ResourceType
from .resource_base import Resource
from .validation_errors import validation_error


class AzureResource(Resource):
    cloud_platform: ClassVar[CloudPlatform] = CloudPlatform.AZURE
    identity: AzureResourceIdentity

    @model_validator(mode="after")
    def validate_native_type(self) -> Self:
        if self.identity.native_type.casefold() != self.resource_type.value.casefold():
            raise validation_error(
                "azure_resource_native_type_mismatch",
                "Azure identity native_type must match resource type",
            )
        return self


class AzureVirtualNetwork(AzureResource):
    resource_type: Literal[ResourceType.AZURE_VIRTUAL_NETWORK] = ResourceType.AZURE_VIRTUAL_NETWORK
    address_spaces: list[IPvAnyNetwork] | None = None


class AzureSubnet(AzureResource):
    resource_type: Literal[ResourceType.AZURE_SUBNET] = ResourceType.AZURE_SUBNET
    address_prefixes: list[IPvAnyNetwork] | None = None


class AzureVirtualMachine(AzureResource):
    resource_type: Literal[ResourceType.AZURE_VIRTUAL_MACHINE] = ResourceType.AZURE_VIRTUAL_MACHINE
    vm_size: str | None = None
