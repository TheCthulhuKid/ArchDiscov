"""Typed ELBv2 resources with ARN-based identities."""

from typing import Literal, Self

from pydantic import JsonValue, field_validator, model_validator

from ..enums import (
    AwsService,
    LoadBalancerProtocol,
    LoadBalancerScheme,
    LoadBalancerType,
    ResourceType,
    TargetType,
)
from ..resource_base import NonEmptyString, Resource
from .load_balancing_sanitization import sanitize_configuration
from .load_balancing_values import ListenerAction, Port, RegisteredTarget, RuleCondition


class _LoadBalancingResource(Resource):
    arn: NonEmptyString

    @model_validator(mode="after")
    def validate_arn_identity(self) -> Self:
        if (
            self.identity.service != AwsService.ELASTIC_LOAD_BALANCING
            or self.identity.physical_id != self.arn
        ):
            raise ValueError(
                "ELBv2 identity must use the resource ARN and elasticloadbalancing service"
            )
        return self


class LoadBalancer(_LoadBalancingResource):
    resource_type: Literal[ResourceType.ELBV2_LOAD_BALANCER] = ResourceType.ELBV2_LOAD_BALANCER
    lb_type: LoadBalancerType | None = None
    scheme: LoadBalancerScheme | None = None
    dns_name: str | None = None
    ip_address_type: str | None = None
    vpc_id: str | None = None
    subnet_ids: list[NonEmptyString] | None = None
    security_group_ids: list[NonEmptyString] | None = None


class Listener(_LoadBalancingResource):
    resource_type: Literal[ResourceType.ELBV2_LISTENER] = ResourceType.ELBV2_LISTENER
    load_balancer_arn: NonEmptyString | None = None
    port: Port | None = None
    protocol: LoadBalancerProtocol | None = None
    actions: list[ListenerAction] | None = None


class ListenerRule(_LoadBalancingResource):
    resource_type: Literal[ResourceType.ELBV2_LISTENER_RULE] = ResourceType.ELBV2_LISTENER_RULE
    listener_arn: NonEmptyString | None = None
    priority: str | None = None
    is_default: bool | None = None
    conditions: list[RuleCondition] | None = None
    actions: list[ListenerAction] | None = None


class TargetGroup(_LoadBalancingResource):
    resource_type: Literal[ResourceType.ELBV2_TARGET_GROUP] = ResourceType.ELBV2_TARGET_GROUP
    vpc_id: str | None = None
    protocol: LoadBalancerProtocol | None = None
    port: Port | None = None
    target_type: TargetType | None = None
    health_check: dict[str, JsonValue] | None = None
    targets: list[RegisteredTarget] | None = None

    @field_validator("health_check")
    @classmethod
    def sanitize_health_check(
        cls, value: dict[str, JsonValue] | None
    ) -> dict[str, JsonValue] | None:
        return None if value is None else sanitize_configuration(value)[0]
