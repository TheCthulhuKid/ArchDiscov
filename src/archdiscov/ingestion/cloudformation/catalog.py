"""CloudFormation-local models and explicit scalar property allowlists."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from ...models.architecture import Ec2Instance, Subnet, Vpc
from ...models.enums import ResourceType
from ...models.resource_base import Resource
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
    model: type[Resource] | None = None
    # (property, target): a typed field on a typed model, else an attribute.
    string_fields: tuple[tuple[str, str], ...] = ()
    integer_fields: tuple[tuple[str, str, int, int], ...] = ()
    # Typed models only. (property, target, AWS spelling -> enum value)
    choices: tuple[tuple[str, str, Mapping[str, Enum]], ...] = ()
    # (property, target): literal identifiers, each cited; a `!Ref` item is a reference.
    string_lists: tuple[tuple[str, str], ...] = ()
    # (property, target): `{Cidr: ...}` entries recorded whole, or not at all.
    cidr_entries: tuple[tuple[str, str], ...] = ()
    # (target, value) the declared type itself states, cited to `Type`.
    constants: tuple[tuple[str, str], ...] = ()


# Both attachment types declare a VPC attachment; each imports as the one attachment model.
_VPC_ATTACHMENT = ResourceMapping(
    TransitGatewayAttachment,
    string_fields=(
        ("TransitGatewayId", "transit_gateway_id"),
        ("VpcId", "attached_resource_id"),
    ),
    constants=(("attached_resource_type", "vpc"),),
)


RESOURCE_MAPPINGS = {
    ResourceType.VPC: ResourceMapping(Vpc),
    ResourceType.SUBNET: ResourceMapping(
        Subnet, string_fields=(("AvailabilityZone", "availability_zone"),)
    ),
    ResourceType.EC2_INSTANCE: ResourceMapping(
        Ec2Instance, string_fields=(("InstanceType", "instance_type"),)
    ),
    ResourceType.SECURITY_GROUP: ResourceMapping(SecurityGroup),
    ResourceType.INTERNET_GATEWAY: ResourceMapping(InternetGateway),
    ResourceType.ROUTE_TABLE: ResourceMapping(RouteTable),
    ResourceType.NETWORK_INTERFACE: ResourceMapping(NetworkInterface),
    ResourceType.NAT_GATEWAY: ResourceMapping(
        NatGateway, string_fields=(("ConnectivityType", "connectivity"),)
    ),
    ResourceType.EGRESS_ONLY_INTERNET_GATEWAY: ResourceMapping(EgressOnlyInternetGateway),
    ResourceType.VPC_ENDPOINT: ResourceMapping(
        VpcEndpoint,
        string_fields=(("ServiceName", "service_name"),),
        choices=(("VpcEndpointType", "endpoint_type", ENDPOINT_TYPES),),
        string_lists=(
            ("RouteTableIds", "route_table_ids"),
            ("SubnetIds", "subnet_ids"),
            ("SecurityGroupIds", "security_group_ids"),
        ),
    ),
    ResourceType.MANAGED_PREFIX_LIST: ResourceMapping(
        ManagedPrefixList,
        string_fields=(("PrefixListName", "name"),),
        choices=(("AddressFamily", "address_family", ADDRESS_FAMILIES),),
        cidr_entries=(("Entries", "entries"),),
    ),
    ResourceType.TRANSIT_GATEWAY: ResourceMapping(
        TransitGateway,
        string_fields=(
            ("AssociationDefaultRouteTableId", "association_default_route_table_id"),
            ("PropagationDefaultRouteTableId", "propagation_default_route_table_id"),
        ),
    ),
    ResourceType.TRANSIT_GATEWAY_ATTACHMENT: _VPC_ATTACHMENT,
    "AWS::EC2::TransitGatewayVpcAttachment": _VPC_ATTACHMENT,
    ResourceType.TRANSIT_GATEWAY_ROUTE_TABLE: ResourceMapping(
        TransitGatewayRouteTable,
        string_fields=(("TransitGatewayId", "transit_gateway_id"),),
    ),
    ResourceType.VPC_PEERING_CONNECTION: ResourceMapping(
        VpcPeeringConnection,
        string_fields=(
            ("VpcId", "requester/vpc_id"),
            ("PeerVpcId", "accepter/vpc_id"),
            ("PeerOwnerId", "accepter/owner_id"),
            ("PeerRegion", "attributes/peer_region"),
        ),
    ),
    ResourceType.LAMBDA_FUNCTION: ResourceMapping(
        string_fields=(
            ("FunctionName", "function_name"),
            ("Runtime", "runtime"),
            ("Role", "role"),
        ),
        integer_fields=(
            ("MemorySize", "memory_size", 128, 10240),
            ("Timeout", "timeout", 1, 900),
        ),
    ),
    ResourceType.SQS_QUEUE: ResourceMapping(
        string_fields=(("QueueName", "queue_name"),),
        integer_fields=(
            ("VisibilityTimeout", "visibility_timeout", 0, 43200),
            ("MessageRetentionPeriod", "message_retention_period", 60, 1209600),
        ),
    ),
    ResourceType.RDS_INSTANCE: ResourceMapping(
        string_fields=(
            ("DBInstanceIdentifier", "db_instance_identifier"),
            ("Engine", "engine"),
            ("EngineVersion", "engine_version"),
            ("DBInstanceClass", "db_instance_class"),
        ),
        integer_fields=(("Port", "port", 1, 65535),),
    ),
    ResourceType.RDS_CLUSTER: ResourceMapping(
        string_fields=(
            ("DBClusterIdentifier", "db_cluster_identifier"),
            ("Engine", "engine"),
            ("EngineVersion", "engine_version"),
        ),
        integer_fields=(("Port", "port", 1, 65535),),
    ),
    ResourceType.RDS_SUBNET_GROUP: ResourceMapping(
        string_fields=(("DBSubnetGroupName", "db_subnet_group_name"),),
    ),
    ResourceType.ECS_CLUSTER: ResourceMapping(string_fields=(("ClusterName", "cluster_name"),)),
    ResourceType.ECS_SERVICE: ResourceMapping(string_fields=(("ServiceName", "service_name"),)),
    ResourceType.ECS_TASK_DEFINITION: ResourceMapping(string_fields=(("Family", "family"),)),
    ResourceType.IAM_ROLE: ResourceMapping(
        string_fields=(("RoleName", "role_name"), ("Path", "path")),
    ),
    ResourceType.INSTANCE_PROFILE: ResourceMapping(
        string_fields=(("InstanceProfileName", "instance_profile_name"), ("Path", "path")),
    ),
    ResourceType.MANAGED_POLICY: ResourceMapping(
        string_fields=(("ManagedPolicyName", "managed_policy_name"), ("Path", "path")),
    ),
    ResourceType.ELBV2_LOAD_BALANCER: ResourceMapping(
        string_fields=(("Name", "name"), ("Scheme", "scheme"), ("Type", "type")),
    ),
    ResourceType.ELBV2_LISTENER: ResourceMapping(
        string_fields=(("Protocol", "protocol"),),
        integer_fields=(("Port", "port", 1, 65535),),
    ),
    ResourceType.ELBV2_TARGET_GROUP: ResourceMapping(
        string_fields=(("Name", "name"), ("Protocol", "protocol"), ("TargetType", "target_type")),
        integer_fields=(("Port", "port", 1, 65535),),
    ),
}
