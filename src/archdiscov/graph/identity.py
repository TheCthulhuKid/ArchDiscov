from dataclasses import dataclass

from archdiscov.models.architecture import UnknownResource
from archdiscov.models.cloud.identity import AzureResourceIdentity
from archdiscov.models.cloud.scope import SupportedScope
from archdiscov.models.enums import CloudPlatform
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.cloud_unknown import CloudUnknownResource


def native_resource_type(resource: Resource) -> str:
    """Return the provider-native type for typed and explicit unknown resources."""
    if isinstance(resource, CloudUnknownResource):
        return resource.native_type
    if isinstance(resource, UnknownResource):
        return resource.aws_resource_type
    return str(resource.resource_type)


@dataclass(frozen=True)
class PhysicalResourceKey:
    """Qualified matching key; a match may still have multiple regional candidates."""

    platform: CloudPlatform
    scope: tuple[tuple[str, str | None], ...]
    native_type: str
    physical_id: str

    def __post_init__(self) -> None:
        if (
            not self.native_type
            or not self.native_type.strip()
            or not self.physical_id
            or not self.physical_id.strip()
        ):
            raise ValueError("physical matching requires a native type and physical identifier")
        object.__setattr__(self, "scope", tuple(sorted(self.scope, key=lambda item: item[0])))
        if self.platform == CloudPlatform.AZURE:
            object.__setattr__(self, "native_type", self.native_type.lower())
            object.__setattr__(self, "physical_id", self.physical_id.lower())

    @classmethod
    def from_values(
        cls, scope: SupportedScope, native_type: str, physical_id: str
    ) -> "PhysicalResourceKey":
        return cls(
            platform=scope.platform,
            scope=tuple(scope.model_dump(mode="json", exclude={"platform"}).items()),
            native_type=native_type,
            physical_id=physical_id,
        )

    @classmethod
    def from_resource(
        cls, scope: SupportedScope, resource: Resource
    ) -> "PhysicalResourceKey | None":
        identity = resource.identity
        if identity.physical_id is None:
            return None
        native_type = native_resource_type(resource)
        physical_id = (
            identity.canonical_arm_id
            if isinstance(identity, AzureResourceIdentity)
            else identity.physical_id
        )
        return cls.from_values(scope, str(native_type), physical_id)
