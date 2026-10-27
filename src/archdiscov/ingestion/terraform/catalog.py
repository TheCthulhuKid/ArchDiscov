"""Terraform-local type mappings; a known AWS type need not have a typed importer."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from ...models.architecture import Ec2Instance, Subnet, Vpc
from ...models.enums import AwsService, ResourceType
from ...models.resource_base import Resource
from ...models.resources.iam import IamInstanceProfile, IamManagedPolicy, IamRole
from ...models.resources.network import (
    EgressOnlyInternetGateway,
    InternetGateway,
    ManagedPrefixList,
    NatGateway,
    NetworkInterface,
    RouteTable,
    SecurityGroup,
    TransitGateway,
    TransitGatewayAttachment,
    TransitGatewayRouteTable,
    VpcEndpoint,
    VpcPeeringConnection,
)
from ..aws_values import ADDRESS_FAMILIES, ENDPOINT_TYPES


@dataclass(frozen=True)
class ResourceMapping:
    service: AwsService
    aws_type: ResourceType | str
    model: type[Resource] | None = None
    string_fields: tuple[tuple[str, str], ...] = ()
    cidr_sources: tuple[str, ...] = ()
    inline_collections: tuple[tuple[str, str], ...] = ()
    # A single IPv4 address recorded as a one-item list, such as a NAT gateway's public IP.
    address_lists: tuple[tuple[str, str], ...] = ()
    # (source, target, AWS spelling -> enum value), such as an endpoint's `Gateway`.
    choices: tuple[tuple[str, str, Mapping[str, Enum]], ...] = ()
    # (source, target): a list of identifiers, each cited to its position.
    string_lists: tuple[tuple[str, str], ...] = ()
    # (source, target, minimum)
    integer_fields: tuple[tuple[str, str, int], ...] = ()
    # (source, target): `{cidr: ...}` blocks recorded whole, or not at all.
    cidr_entries: tuple[tuple[str, str], ...] = ()
    # (target, value) the Terraform type itself states, cited to the type.
    constants: tuple[tuple[str, str], ...] = ()


RESOURCE_MAPPINGS = {
    "aws_vpc": ResourceMapping(
        AwsService.EC2, ResourceType.VPC, Vpc, cidr_sources=("cidr_block", "ipv6_cidr_block")
    ),
    "aws_subnet": ResourceMapping(
        AwsService.EC2,
        ResourceType.SUBNET,
        Subnet,
        string_fields=(("availability_zone", "availability_zone"),),
        cidr_sources=("cidr_block", "ipv6_cidr_block"),
    ),
    "aws_instance": ResourceMapping(
        AwsService.EC2,
        ResourceType.EC2_INSTANCE,
        Ec2Instance,
        string_fields=(("instance_type", "instance_type"),),
    ),
    "aws_security_group": ResourceMapping(
        AwsService.EC2,
        ResourceType.SECURITY_GROUP,
        SecurityGroup,
        inline_collections=(("ingress", "ingress_rules"), ("egress", "egress_rules")),
    ),
    "aws_internet_gateway": ResourceMapping(
        AwsService.EC2, ResourceType.INTERNET_GATEWAY, InternetGateway
    ),
    "aws_route_table": ResourceMapping(
        AwsService.EC2,
        ResourceType.ROUTE_TABLE,
        RouteTable,
        inline_collections=(("route", "routes"),),
    ),
    "aws_network_interface": ResourceMapping(
        AwsService.EC2, ResourceType.NETWORK_INTERFACE, NetworkInterface
    ),
    "aws_iam_role": ResourceMapping(
        AwsService.IAM,
        ResourceType.IAM_ROLE,
        IamRole,
        string_fields=(("permissions_boundary", "boundary_arn"),),
    ),
    "aws_iam_instance_profile": ResourceMapping(
        AwsService.IAM, ResourceType.INSTANCE_PROFILE, IamInstanceProfile
    ),
    "aws_iam_policy": ResourceMapping(
        AwsService.IAM, ResourceType.MANAGED_POLICY, IamManagedPolicy
    ),
    "aws_s3_bucket": ResourceMapping(AwsService.S3, "AWS::S3::Bucket"),
    "aws_lambda_function": ResourceMapping(AwsService.LAMBDA, ResourceType.LAMBDA_FUNCTION),
    "aws_sqs_queue": ResourceMapping(AwsService.SQS, ResourceType.SQS_QUEUE),
    "aws_db_instance": ResourceMapping(AwsService.RDS, ResourceType.RDS_INSTANCE),
    "aws_rds_cluster": ResourceMapping(AwsService.RDS, ResourceType.RDS_CLUSTER),
    "aws_db_subnet_group": ResourceMapping(AwsService.RDS, ResourceType.RDS_SUBNET_GROUP),
    "aws_ecs_cluster": ResourceMapping(AwsService.ECS, ResourceType.ECS_CLUSTER),
    "aws_ecs_service": ResourceMapping(AwsService.ECS, ResourceType.ECS_SERVICE),
    "aws_ecs_task_definition": ResourceMapping(AwsService.ECS, ResourceType.ECS_TASK_DEFINITION),
    "aws_lb": ResourceMapping(AwsService.ELASTIC_LOAD_BALANCING, ResourceType.ELBV2_LOAD_BALANCER),
    "aws_lb_listener": ResourceMapping(
        AwsService.ELASTIC_LOAD_BALANCING, ResourceType.ELBV2_LISTENER
    ),
    "aws_lb_target_group": ResourceMapping(
        AwsService.ELASTIC_LOAD_BALANCING, ResourceType.ELBV2_TARGET_GROUP
    ),
    "aws_iam_user": ResourceMapping(AwsService.IAM, "AWS::IAM::User"),
    "aws_iam_group": ResourceMapping(AwsService.IAM, "AWS::IAM::Group"),
    "aws_network_acl": ResourceMapping(AwsService.EC2, ResourceType.NETWORK_ACL),
    "aws_nat_gateway": ResourceMapping(
        AwsService.EC2,
        ResourceType.NAT_GATEWAY,
        NatGateway,
        string_fields=(
            ("connectivity_type", "connectivity"),
            ("network_interface_id", "network_interface_id"),
        ),
        address_lists=(("public_ip", "public_ips"),),
    ),
    "aws_vpc_peering_connection": ResourceMapping(
        AwsService.EC2,
        ResourceType.VPC_PEERING_CONNECTION,
        VpcPeeringConnection,
        string_fields=(
            ("vpc_id", "requester/vpc_id"),
            ("peer_vpc_id", "accepter/vpc_id"),
            ("peer_owner_id", "accepter/owner_id"),
            ("peer_region", "attributes/peer_region"),
            ("accept_status", "state"),
        ),
    ),
    "aws_vpc_endpoint": ResourceMapping(
        AwsService.EC2,
        ResourceType.VPC_ENDPOINT,
        VpcEndpoint,
        string_fields=(
            ("service_name", "service_name"),
            ("state", "state"),
            ("owner_id", "owner_id"),
        ),
        choices=(("vpc_endpoint_type", "endpoint_type", ENDPOINT_TYPES),),
        string_lists=(
            ("route_table_ids", "route_table_ids"),
            ("subnet_ids", "subnet_ids"),
            ("network_interface_ids", "network_interface_ids"),
            ("security_group_ids", "security_group_ids"),
        ),
    ),
    "aws_ec2_managed_prefix_list": ResourceMapping(
        AwsService.EC2,
        ResourceType.MANAGED_PREFIX_LIST,
        ManagedPrefixList,
        string_fields=(("owner_id", "owner_id"),),
        choices=(("address_family", "address_family", ADDRESS_FAMILIES),),
        integer_fields=(("version", "version", 1),),
        cidr_entries=(("entry", "entries"),),
    ),
    "aws_ec2_transit_gateway": ResourceMapping(
        AwsService.EC2,
        ResourceType.TRANSIT_GATEWAY,
        TransitGateway,
        string_fields=(
            ("owner_id", "owner_id"),
            ("association_default_route_table_id", "association_default_route_table_id"),
            ("propagation_default_route_table_id", "propagation_default_route_table_id"),
        ),
    ),
    "aws_ec2_transit_gateway_vpc_attachment": ResourceMapping(
        AwsService.EC2,
        ResourceType.TRANSIT_GATEWAY_ATTACHMENT,
        TransitGatewayAttachment,
        string_fields=(
            ("transit_gateway_id", "transit_gateway_id"),
            ("vpc_id", "attached_resource_id"),
            ("vpc_owner_id", "attached_resource_owner_id"),
        ),
        constants=(("attached_resource_type", "vpc"),),
    ),
    "aws_ec2_transit_gateway_route_table": ResourceMapping(
        AwsService.EC2,
        ResourceType.TRANSIT_GATEWAY_ROUTE_TABLE,
        TransitGatewayRouteTable,
        string_fields=(("transit_gateway_id", "transit_gateway_id"),),
    ),
    "aws_egress_only_internet_gateway": ResourceMapping(
        AwsService.EC2, ResourceType.EGRESS_ONLY_INTERNET_GATEWAY, EgressOnlyInternetGateway
    ),
    "aws_eip": ResourceMapping(AwsService.EC2, "AWS::EC2::EIP"),
}


def service_for_type(terraform_type: str) -> AwsService:
    mapping = RESOURCE_MAPPINGS.get(terraform_type)
    if mapping is not None:
        return mapping.service
    for prefix, service in (
        ("aws_iam_", AwsService.IAM),
        ("aws_s3_", AwsService.S3),
        ("aws_lambda_", AwsService.LAMBDA),
        ("aws_sqs_", AwsService.SQS),
        ("aws_db_", AwsService.RDS),
        ("aws_rds_", AwsService.RDS),
        ("aws_ecs_", AwsService.ECS),
        ("aws_lb", AwsService.ELASTIC_LOAD_BALANCING),
        ("aws_alb", AwsService.ELASTIC_LOAD_BALANCING),
        ("aws_ec2_", AwsService.EC2),
    ):
        if terraform_type.startswith(prefix):
            return service
    return AwsService.UNKNOWN
