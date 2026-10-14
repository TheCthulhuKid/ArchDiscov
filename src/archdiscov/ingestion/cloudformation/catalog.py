"""CloudFormation-local models and explicit scalar property allowlists."""

from dataclasses import dataclass

from ...models.architecture import Ec2Instance, Subnet, Vpc
from ...models.enums import ResourceType
from ...models.network import InternetGateway, NetworkInterface, RouteTable, SecurityGroup
from ...models.resource_base import Resource


@dataclass(frozen=True)
class ResourceMapping:
    model: type[Resource] | None = None
    string_fields: tuple[tuple[str, str], ...] = ()
    integer_fields: tuple[tuple[str, str, int, int], ...] = ()


RESOURCE_MAPPINGS = {
    ResourceType.VPC: ResourceMapping(Vpc),
    ResourceType.SUBNET: ResourceMapping(Subnet),
    ResourceType.EC2_INSTANCE: ResourceMapping(Ec2Instance),
    ResourceType.SECURITY_GROUP: ResourceMapping(SecurityGroup),
    ResourceType.INTERNET_GATEWAY: ResourceMapping(InternetGateway),
    ResourceType.ROUTE_TABLE: ResourceMapping(RouteTable),
    ResourceType.NETWORK_INTERFACE: ResourceMapping(NetworkInterface),
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
