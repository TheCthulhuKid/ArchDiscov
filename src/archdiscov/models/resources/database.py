"""RDS configuration facts with source-ARN identities, not reachability conclusions."""

import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ..enums import AwsService, ResourceType
from ..resource_base import Model, NonEmptyString, Resource

Port = Annotated[int, Field(ge=1, le=65535, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
EndpointAddress = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$")]


def rds_arn_matches(value: object, kind: str, account_id: str, region: str) -> bool:
    name = r"[A-Za-z0-9][A-Za-z0-9._ -]*" if kind == "subgrp" else r"[A-Za-z0-9][A-Za-z0-9-]*"
    pattern = rf"arn:aws(?:-[a-z0-9]+)*:rds:{re.escape(region)}:{account_id}:{kind}:{name}"
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


class DbClusterMember(Model):
    db_instance_identifier: NonEmptyString
    is_cluster_writer: Annotated[bool, Field(strict=True)] | None = None
    promotion_tier: NonNegativeInt | None = None


class _DatabaseResource(Resource):
    arn: NonEmptyString
    status: NonEmptyString | None = None
    vpc_id: NonEmptyString | None = None
    subnet_ids: list[NonEmptyString] | None = None
    security_group_ids: list[NonEmptyString] | None = None
    associated_role_arns: list[NonEmptyString] | None = None

    @model_validator(mode="after")
    def validate_arn_identity(self) -> Self:
        kind = {
            ResourceType.RDS_INSTANCE: "db",
            ResourceType.RDS_CLUSTER: "cluster",
            ResourceType.RDS_SUBNET_GROUP: "subgrp",
        }[self.resource_type]
        if (
            self.identity.service != AwsService.RDS
            or self.identity.physical_id != self.arn
            or not rds_arn_matches(self.arn, kind, self.identity.account_id, self.identity.region)
        ):
            raise ValueError(
                "RDS identity must use the matching source ARN, scope, and rds service"
            )
        return self


class _DatabaseEngineResource(_DatabaseResource):
    identifier: NonEmptyString | None = None
    engine: NonEmptyString | None = None
    engine_version: NonEmptyString | None = None
    endpoint_address: EndpointAddress | None = None
    endpoint_port: Port | None = None
    db_subnet_group_name: NonEmptyString | None = None
    storage_encrypted: Annotated[bool, Field(strict=True)] | None = None
    publicly_accessible: Annotated[bool, Field(strict=True)] | None = None
    allocated_storage: NonNegativeInt | None = None
    storage_type: NonEmptyString | None = None
    iops: NonNegativeInt | None = None
    storage_throughput: NonNegativeInt | None = None
    backup_retention_period: NonNegativeInt | None = None
    deletion_protection: Annotated[bool, Field(strict=True)] | None = None
    multi_az: Annotated[bool, Field(strict=True)] | None = None


class DbInstance(_DatabaseEngineResource):
    resource_type: Literal[ResourceType.RDS_INSTANCE] = ResourceType.RDS_INSTANCE
    instance_class: NonEmptyString | None = None
    availability_zone: NonEmptyString | None = None
    cluster_identifier: NonEmptyString | None = None


class DbCluster(_DatabaseEngineResource):
    resource_type: Literal[ResourceType.RDS_CLUSTER] = ResourceType.RDS_CLUSTER
    reader_endpoint_address: EndpointAddress | None = None
    availability_zones: list[NonEmptyString] | None = None
    members: list[DbClusterMember] | None = None


class DbSubnetGroup(_DatabaseResource):
    resource_type: Literal[ResourceType.RDS_SUBNET_GROUP] = ResourceType.RDS_SUBNET_GROUP
    group_name: NonEmptyString | None = None
