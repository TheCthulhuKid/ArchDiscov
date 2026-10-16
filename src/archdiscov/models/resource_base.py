from typing import Annotated, Any, ClassVar, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from .cloud.identity import ResourceIdentity, SupportedIdentity, parse_identity  # noqa: F401
from .enums import CloudPlatform, LifecycleAction, LifecycleValueScope, ResourceType
from .sanitization import sanitize_value

NonEmptyString = Annotated[str, Field(min_length=1, pattern=r"\S")]
FactPath = Annotated[str, Field(pattern=r"^/(?:[^~]|~[01])*$")]
EvidenceIds = Annotated[list[NonEmptyString], Field(min_length=1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("attributes", "metadata", check_fields=False)
    @classmethod
    def sanitize_generic_values(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        sanitized, _ = sanitize_value(value)
        return sanitized


_LIFECYCLE_SEQUENCES = {
    (LifecycleAction.NO_OP,),
    (LifecycleAction.CREATE,),
    (LifecycleAction.READ,),
    (LifecycleAction.UPDATE,),
    (LifecycleAction.DELETE,),
    (LifecycleAction.DELETE, LifecycleAction.CREATE),
    (LifecycleAction.CREATE, LifecycleAction.DELETE),
}


class ResourceLifecycle(Model):
    """Planned change for a resource (schema 2, Terraform plans only)."""

    actions: list[LifecycleAction] = Field(min_length=1)
    value_scope: LifecycleValueScope | None = None

    @model_serializer(mode="wrap")
    def omit_absent_scope(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if self.value_scope is None:
            data.pop("value_scope", None)
        return data

    @model_validator(mode="after")
    def validate_action_sequence(self) -> Self:
        if tuple(self.actions) not in _LIFECYCLE_SEQUENCES:
            raise ValueError("unsupported lifecycle action sequence")
        if self.value_scope is not None:
            expected = (
                LifecycleValueScope.BEFORE
                if self.actions == [LifecycleAction.DELETE]
                else LifecycleValueScope.AFTER
            )
            if self.value_scope != expected:
                raise ValueError("lifecycle value_scope does not match its action sequence")
        return self


class Resource(Model):
    """Common resource data and a recursively typed containment tree."""

    cloud_platform: ClassVar[CloudPlatform | None] = CloudPlatform.AWS
    id: str
    identity: SupportedIdentity
    name: str | None = None
    resource_type: ResourceType
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    fact_evidence: dict[FactPath, EvidenceIds] = Field(default_factory=dict)
    children: list["SupportedResource"] = Field(default_factory=list)  # noqa: F821
    lifecycle: ResourceLifecycle | None = None  # schema 2; omitted from output when absent

    @model_serializer(mode="wrap")
    def omit_absent_lifecycle(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if self.lifecycle is None:
            data.pop("lifecycle", None)
        return data

    @model_validator(mode="before")
    @classmethod
    def supply_stable_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and "id" not in data and "identity" in data:
            identity = parse_identity(data["identity"])
            return {**data, "id": identity.stable_id}
        return data

    @model_validator(mode="after")
    def validate_stable_id(self) -> Self:
        if self.cloud_platform is not None and self.identity.platform != self.cloud_platform:
            raise ValueError("resource identity platform must match resource type")
        if self.id != self.identity.stable_id:
            raise ValueError("resource id must match identity.stable_id")
        return self
