"""Typed listener actions, rule conditions, and target registrations."""

from typing import Annotated

from pydantic import Field, JsonValue, ValidationInfo, field_validator

from ..enums import ListenerActionType, TargetHealthState
from ..resource_base import Model, NonEmptyString
from .load_balancing_sanitization import sanitize_configuration

Port = Annotated[int, Field(ge=1, le=65535)]


class WeightedTargetGroup(Model):
    arn: NonEmptyString
    weight: int | None = Field(default=None, ge=0, le=999)


class ListenerAction(Model):
    type: ListenerActionType
    order: int | None = None
    target_group_arn: NonEmptyString | None = None
    forward_groups: list[WeightedTargetGroup] = Field(default_factory=list)
    configuration: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("configuration")
    @classmethod
    def sanitize_action(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return sanitize_configuration(value)[0]


class RuleCondition(Model):
    field: NonEmptyString
    configuration: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("configuration")
    @classmethod
    def sanitize_condition(
        cls, value: dict[str, JsonValue], info: ValidationInfo
    ) -> dict[str, JsonValue]:
        return sanitize_configuration(value, condition_field=info.data.get("field"))[0]


class RegisteredTarget(Model):
    id: NonEmptyString
    port: Port | None = None
    availability_zone: str | None = None
    health_state: TargetHealthState | None = None
    health_reason: str | None = None
    health_description: str | None = None
