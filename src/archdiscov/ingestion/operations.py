"""AWS read operations the collector may call; anything else is rejected before a client call."""

from enum import StrEnum

from ..models.enums import AwsService


class Ec2Operation(StrEnum):
    DESCRIBE_REGIONS = "DescribeRegions"
    DESCRIBE_VPCS = "DescribeVpcs"
    DESCRIBE_SUBNETS = "DescribeSubnets"
    DESCRIBE_INSTANCES = "DescribeInstances"
    DESCRIBE_ROUTE_TABLES = "DescribeRouteTables"
    DESCRIBE_INTERNET_GATEWAYS = "DescribeInternetGateways"
    DESCRIBE_NETWORK_ACLS = "DescribeNetworkAcls"
    DESCRIBE_SECURITY_GROUPS = "DescribeSecurityGroups"
    DESCRIBE_SECURITY_GROUP_RULES = "DescribeSecurityGroupRules"
    DESCRIBE_NETWORK_INTERFACES = "DescribeNetworkInterfaces"
    DESCRIBE_NAT_GATEWAYS = "DescribeNatGateways"
    DESCRIBE_EGRESS_ONLY_INTERNET_GATEWAYS = "DescribeEgressOnlyInternetGateways"
    DESCRIBE_VPC_PEERING_CONNECTIONS = "DescribeVpcPeeringConnections"
    DESCRIBE_VPC_ENDPOINTS = "DescribeVpcEndpoints"
    DESCRIBE_MANAGED_PREFIX_LISTS = "DescribeManagedPrefixLists"
    GET_MANAGED_PREFIX_LIST_ENTRIES = "GetManagedPrefixListEntries"
    DESCRIBE_TRANSIT_GATEWAYS = "DescribeTransitGateways"
    DESCRIBE_TRANSIT_GATEWAY_ATTACHMENTS = "DescribeTransitGatewayAttachments"
    DESCRIBE_TRANSIT_GATEWAY_VPC_ATTACHMENTS = "DescribeTransitGatewayVpcAttachments"
    DESCRIBE_TRANSIT_GATEWAY_ROUTE_TABLES = "DescribeTransitGatewayRouteTables"
    GET_TRANSIT_GATEWAY_ROUTE_TABLE_ASSOCIATIONS = "GetTransitGatewayRouteTableAssociations"
    GET_TRANSIT_GATEWAY_ROUTE_TABLE_PROPAGATIONS = "GetTransitGatewayRouteTablePropagations"
    SEARCH_TRANSIT_GATEWAY_ROUTES = "SearchTransitGatewayRoutes"


class IamOperation(StrEnum):
    LIST_ROLES = "ListRoles"
    GET_ROLE = "GetRole"
    LIST_INSTANCE_PROFILES = "ListInstanceProfiles"
    LIST_ATTACHED_ROLE_POLICIES = "ListAttachedRolePolicies"
    LIST_ROLE_POLICIES = "ListRolePolicies"
    GET_ROLE_POLICY = "GetRolePolicy"
    GET_POLICY = "GetPolicy"
    GET_POLICY_VERSION = "GetPolicyVersion"


class Elbv2Operation(StrEnum):
    DESCRIBE_LOAD_BALANCERS = "DescribeLoadBalancers"
    DESCRIBE_LISTENERS = "DescribeListeners"
    DESCRIBE_RULES = "DescribeRules"
    DESCRIBE_TARGET_GROUPS = "DescribeTargetGroups"
    DESCRIBE_TARGET_HEALTH = "DescribeTargetHealth"
    DESCRIBE_TAGS = "DescribeTags"


class EcsOperation(StrEnum):
    LIST_CLUSTERS = "ListClusters"
    DESCRIBE_CLUSTERS = "DescribeClusters"
    LIST_SERVICES = "ListServices"
    DESCRIBE_SERVICES = "DescribeServices"
    LIST_TASKS = "ListTasks"
    DESCRIBE_TASKS = "DescribeTasks"
    DESCRIBE_TASK_DEFINITION = "DescribeTaskDefinition"


class LambdaOperation(StrEnum):
    LIST_FUNCTIONS = "ListFunctions"
    GET_FUNCTION_CONFIGURATION = "GetFunctionConfiguration"


class RdsOperation(StrEnum):
    DESCRIBE_DB_INSTANCES = "DescribeDBInstances"
    DESCRIBE_DB_CLUSTERS = "DescribeDBClusters"
    DESCRIBE_DB_SUBNET_GROUPS = "DescribeDBSubnetGroups"


class SqsOperation(StrEnum):
    LIST_QUEUES = "ListQueues"
    GET_QUEUE_ATTRIBUTES = "GetQueueAttributes"
    LIST_QUEUE_TAGS = "ListQueueTags"


ALLOWED_OPERATIONS: frozenset[tuple[AwsService, str]] = frozenset(
    {
        (AwsService.STS, "GetCallerIdentity"),
        (AwsService.STS, "AssumeRole"),
        *((AwsService.EC2, operation.value) for operation in Ec2Operation),
        *((AwsService.IAM, operation.value) for operation in IamOperation),
        *((AwsService.ELASTIC_LOAD_BALANCING, operation.value) for operation in Elbv2Operation),
        *((AwsService.ECS, operation.value) for operation in EcsOperation),
        *((AwsService.LAMBDA, operation.value) for operation in LambdaOperation),
        *((AwsService.RDS, operation.value) for operation in RdsOperation),
        *((AwsService.SQS, operation.value) for operation in SqsOperation),
    }
)
