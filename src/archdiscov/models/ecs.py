"""ECS configuration models; workload environment and executable content are excluded."""

from typing import Literal, Self

from pydantic import Field, model_validator

from .enums import AwsService, ResourceType
from .resource_base import Model, NonEmptyString, Resource


class EcsPortMapping(Model):
    container_port: int | None = Field(default=None, ge=0, le=65535)
    host_port: int | None = Field(default=None, ge=0, le=65535)
    protocol: str | None = None
    app_protocol: str | None = None
    container_port_range: str | None = None


class EcsContainerDefinition(Model):
    name: NonEmptyString | None = None
    image: NonEmptyString | None = None
    cpu: int | None = Field(default=None, ge=0)
    memory: int | None = Field(default=None, ge=0)
    memory_reservation: int | None = Field(default=None, ge=0)
    essential: bool | None = None
    port_mappings: list[EcsPortMapping] | None = None


class _EcsResource(Resource):
    arn: NonEmptyString

    @model_validator(mode="after")
    def validate_arn_identity(self) -> Self:
        if self.identity.service != AwsService.ECS or self.identity.physical_id != self.arn:
            raise ValueError("ECS identity must use the resource ARN and ecs service")
        return self


class EcsCluster(_EcsResource):
    resource_type: Literal[ResourceType.ECS_CLUSTER] = ResourceType.ECS_CLUSTER
    status: str | None = None
    capacity_providers: list[NonEmptyString] | None = None
    running_tasks_count: int | None = Field(default=None, ge=0)
    pending_tasks_count: int | None = Field(default=None, ge=0)
    active_services_count: int | None = Field(default=None, ge=0)


class EcsService(_EcsResource):
    resource_type: Literal[ResourceType.ECS_SERVICE] = ResourceType.ECS_SERVICE
    cluster_arn: NonEmptyString | None = None
    task_definition_arn: NonEmptyString | None = None
    status: str | None = None
    desired_count: int | None = Field(default=None, ge=0)
    running_count: int | None = Field(default=None, ge=0)
    pending_count: int | None = Field(default=None, ge=0)
    launch_type: str | None = None
    scheduling_strategy: str | None = None
    platform_version: str | None = None
    subnet_ids: list[NonEmptyString] | None = None
    security_group_ids: list[NonEmptyString] | None = None
    assign_public_ip: str | None = None


class EcsTask(_EcsResource):
    resource_type: Literal[ResourceType.ECS_TASK] = ResourceType.ECS_TASK
    cluster_arn: NonEmptyString | None = None
    task_definition_arn: NonEmptyString | None = None
    desired_status: str | None = None
    last_status: str | None = None
    launch_type: str | None = None
    platform_version: str | None = None
    availability_zone: str | None = None
    cpu: str | None = None
    memory: str | None = None
    subnet_ids: list[NonEmptyString] | None = None
    security_group_ids: list[NonEmptyString] | None = None
    network_interface_ids: list[NonEmptyString] | None = None


class EcsTaskDefinition(_EcsResource):
    resource_type: Literal[ResourceType.ECS_TASK_DEFINITION] = ResourceType.ECS_TASK_DEFINITION
    execution_role_arn: NonEmptyString | None = None
    task_role_arn: NonEmptyString | None = None
    family: NonEmptyString | None = None
    revision: int | None = Field(default=None, ge=1)
    status: str | None = None
    network_mode: str | None = None
    cpu: str | None = None
    memory: str | None = None
    requires_compatibilities: list[NonEmptyString] | None = None
    containers: list[EcsContainerDefinition] | None = None
