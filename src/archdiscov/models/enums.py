from enum import StrEnum


class CloudPlatform(StrEnum):
    AWS = "aws"
    AZURE = "azure"


class AnalyzerKind(StrEnum):
    OWNERSHIP = "ownership"
    SECURITY_GROUP_IMPACT = "security_group_impact"
    CROSS_ACCOUNT_TRUST = "cross_account_trust"
    DATABASE_ACCESS = "database_access"
    INTERNET_EXPOSURE = "internet_exposure"


class AwsPartition(StrEnum):
    AWS = "aws"
    AWS_CN = "aws-cn"
    AWS_US_GOV = "aws-us-gov"


class AwsRegion(StrEnum):
    """AWS region codes and account-wide scope; availability depends on the account."""

    GLOBAL = "global"

    # Commercial regions (aws).
    AF_SOUTH_1 = "af-south-1"
    AP_EAST_1 = "ap-east-1"
    AP_EAST_2 = "ap-east-2"
    AP_NORTHEAST_1 = "ap-northeast-1"
    AP_NORTHEAST_2 = "ap-northeast-2"
    AP_NORTHEAST_3 = "ap-northeast-3"
    AP_SOUTH_1 = "ap-south-1"
    AP_SOUTH_2 = "ap-south-2"
    AP_SOUTHEAST_1 = "ap-southeast-1"
    AP_SOUTHEAST_2 = "ap-southeast-2"
    AP_SOUTHEAST_3 = "ap-southeast-3"
    AP_SOUTHEAST_4 = "ap-southeast-4"
    AP_SOUTHEAST_5 = "ap-southeast-5"
    AP_SOUTHEAST_6 = "ap-southeast-6"
    AP_SOUTHEAST_7 = "ap-southeast-7"
    CA_CENTRAL_1 = "ca-central-1"
    CA_WEST_1 = "ca-west-1"
    EU_CENTRAL_1 = "eu-central-1"
    EU_CENTRAL_2 = "eu-central-2"
    EU_NORTH_1 = "eu-north-1"
    EU_SOUTH_1 = "eu-south-1"
    EU_SOUTH_2 = "eu-south-2"
    EU_WEST_1 = "eu-west-1"
    EU_WEST_2 = "eu-west-2"
    EU_WEST_3 = "eu-west-3"
    IL_CENTRAL_1 = "il-central-1"
    ME_CENTRAL_1 = "me-central-1"
    ME_SOUTH_1 = "me-south-1"
    MX_CENTRAL_1 = "mx-central-1"
    SA_EAST_1 = "sa-east-1"
    US_EAST_1 = "us-east-1"
    US_EAST_2 = "us-east-2"
    US_WEST_1 = "us-west-1"
    US_WEST_2 = "us-west-2"

    # China regions (aws-cn).
    CN_NORTH_1 = "cn-north-1"
    CN_NORTHWEST_1 = "cn-northwest-1"

    # GovCloud regions (aws-us-gov).
    US_GOV_EAST_1 = "us-gov-east-1"
    US_GOV_WEST_1 = "us-gov-west-1"

    # Isolated regions (aws-iso, aws-iso-b, aws-iso-e, aws-iso-f).
    US_ISO_EAST_1 = "us-iso-east-1"
    US_ISO_WEST_1 = "us-iso-west-1"
    US_ISOB_EAST_1 = "us-isob-east-1"
    US_ISOB_WEST_1 = "us-isob-west-1"
    EU_ISOE_WEST_1 = "eu-isoe-west-1"
    US_ISOF_EAST_1 = "us-isof-east-1"
    US_ISOF_SOUTH_1 = "us-isof-south-1"

    # European Sovereign Cloud (aws-eusc).
    EUSC_DE_EAST_1 = "eusc-de-east-1"


class AwsService(StrEnum):
    """AWS ARN service namespaces used by resource identities, not Boto3 client names."""

    EC2 = "ec2"
    ELASTIC_LOAD_BALANCING = "elasticloadbalancing"
    ECS = "ecs"
    LAMBDA = "lambda"
    RDS = "rds"
    SQS = "sqs"
    IAM = "iam"
    STS = "sts"
    CLOUDFORMATION = "cloudformation"
    S3 = "s3"
    UNKNOWN = "unknown"


class DependencyRelationship(StrEnum):
    """Normalized relationship kinds between resources."""

    ATTACHED_TO = "attached_to"
    FORWARDS_TO = "forwards_to"
    ASSUMES_ROLE = "assumes_role"


class LifecycleAction(StrEnum):
    """Terraform plan actions retained in order; replacement is two actions, not an update."""

    NO_OP = "no-op"
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"


class LifecycleValueScope(StrEnum):
    BEFORE = "before"
    AFTER = "after"


class SourceType(StrEnum):
    """Source families for evidence, independent of snapshot source kinds."""

    AZURE = "azure"

    AWS = "aws"
    TERRAFORM = "terraform"
    CLOUDFORMATION = "cloudformation"
    MOCK = "mock"


class SnapshotSourceKind(StrEnum):
    """Snapshot inputs, distinguishing Terraform state from planned changes."""

    AWS = "aws"
    AZURE_FIXTURE = "azure_fixture"
    TERRAFORM_STATE = "terraform_state"
    TERRAFORM_PLAN = "terraform_plan"
    CLOUDFORMATION = "cloudformation"
    MOCK = "mock"


class CoverageStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    INCOMPLETE = "incomplete"


class EvidenceValueStatus(StrEnum):
    PRESENT = "present"
    REDACTED = "redacted"
    OMITTED = "omitted"
    UNRESOLVED = "unresolved"


class AnalysisStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ResourceType(StrEnum):
    VPC = "AWS::EC2::VPC"
    SUBNET = "AWS::EC2::Subnet"
    EC2_INSTANCE = "AWS::EC2::Instance"
    ROUTE_TABLE = "AWS::EC2::RouteTable"
    INTERNET_GATEWAY = "AWS::EC2::InternetGateway"
    NETWORK_ACL = "AWS::EC2::NetworkAcl"
    SECURITY_GROUP = "AWS::EC2::SecurityGroup"
    NETWORK_INTERFACE = "AWS::EC2::NetworkInterface"
    NAT_GATEWAY = "AWS::EC2::NatGateway"
    EGRESS_ONLY_INTERNET_GATEWAY = "AWS::EC2::EgressOnlyInternetGateway"
    VPC_PEERING_CONNECTION = "AWS::EC2::VPCPeeringConnection"
    TRANSIT_GATEWAY = "AWS::EC2::TransitGateway"
    TRANSIT_GATEWAY_ATTACHMENT = "AWS::EC2::TransitGatewayAttachment"
    TRANSIT_GATEWAY_ROUTE_TABLE = "AWS::EC2::TransitGatewayRouteTable"
    VPC_ENDPOINT = "AWS::EC2::VPCEndpoint"
    MANAGED_PREFIX_LIST = "AWS::EC2::PrefixList"
    IAM_ROLE = "AWS::IAM::Role"
    INSTANCE_PROFILE = "AWS::IAM::InstanceProfile"
    MANAGED_POLICY = "AWS::IAM::ManagedPolicy"
    ELBV2_LOAD_BALANCER = "AWS::ElasticLoadBalancingV2::LoadBalancer"
    ELBV2_LISTENER = "AWS::ElasticLoadBalancingV2::Listener"
    ELBV2_LISTENER_RULE = "AWS::ElasticLoadBalancingV2::ListenerRule"
    ELBV2_TARGET_GROUP = "AWS::ElasticLoadBalancingV2::TargetGroup"
    ECS_CLUSTER = "AWS::ECS::Cluster"
    ECS_SERVICE = "AWS::ECS::Service"
    ECS_TASK = "AWS::ECS::Task"
    ECS_TASK_DEFINITION = "AWS::ECS::TaskDefinition"
    LAMBDA_FUNCTION = "AWS::Lambda::Function"
    RDS_INSTANCE = "AWS::RDS::DBInstance"
    RDS_CLUSTER = "AWS::RDS::DBCluster"
    RDS_SUBNET_GROUP = "AWS::RDS::DBSubnetGroup"
    SQS_QUEUE = "AWS::SQS::Queue"
    UNKNOWN = "unknown"
    AZURE_VIRTUAL_NETWORK = "Microsoft.Network/virtualNetworks"
    AZURE_SUBNET = "Microsoft.Network/virtualNetworks/subnets"
    AZURE_VIRTUAL_MACHINE = "Microsoft.Compute/virtualMachines"
    CLOUD_UNKNOWN = "cloud_unknown"


class AttachmentKind(StrEnum):
    """Value of attributes["kind"] on attached_to dependencies."""

    ROUTE_TABLE_ASSOCIATION = "route_table_association"
    INTERNET_GATEWAY_ATTACHMENT = "internet_gateway_attachment"
    NETWORK_ACL_ASSOCIATION = "network_acl_association"
    SECURITY_GROUP_MEMBERSHIP = "security_group_membership"
    SECURITY_GROUP_REFERENCE = "security_group_reference"
    ROUTE_TARGET = "route_target"
    NAT_GATEWAY_SUBNET = "nat_gateway_subnet"
    NAT_GATEWAY_INTERFACE = "nat_gateway_interface"
    PEERING_REQUESTER_VPC = "peering_requester_vpc"
    PEERING_ACCEPTER_VPC = "peering_accepter_vpc"
    TRANSIT_GATEWAY_ATTACHMENT = "transit_gateway_attachment"
    TRANSIT_GATEWAY_VPC_ATTACHMENT = "transit_gateway_vpc_attachment"
    TRANSIT_GATEWAY_ATTACHMENT_SUBNET = "transit_gateway_attachment_subnet"
    TRANSIT_GATEWAY_ROUTE_TABLE_ASSOCIATION = "transit_gateway_route_table_association"
    TRANSIT_GATEWAY_ROUTE_TABLE_PROPAGATION = "transit_gateway_route_table_propagation"
    ENDPOINT_ROUTE_TABLE = "endpoint_route_table"
    ENDPOINT_SUBNET = "endpoint_subnet"
    ENDPOINT_INTERFACE = "endpoint_interface"
    ENDPOINT_SECURITY_GROUP = "endpoint_security_group"
    EGRESS_ONLY_INTERNET_GATEWAY_ATTACHMENT = "egress_only_internet_gateway_attachment"
    PREFIX_LIST_REFERENCE = "prefix_list_reference"
    INSTANCE_INTERFACE = "instance_interface"
    INSTANCE_PROFILE = "instance_profile"
    PROFILE_ROLE = "profile_role"
    ROLE_POLICY = "role_policy"
    PERMISSIONS_BOUNDARY = "permissions_boundary"
    TRUST_PRINCIPAL = "trust_principal"
    SUBNET_MEMBERSHIP = "subnet_membership"
    LISTENER_LOAD_BALANCER = "listener_load_balancer"
    RULE_LISTENER = "rule_listener"
    CLUSTER_MEMBERSHIP = "cluster_membership"
    TASK_DEFINITION = "task_definition"
    EXECUTION_ROLE = "execution_role"
    TASK_ROLE = "task_role"
    FUNCTION_ROLE = "function_role"
    DATABASE_CLUSTER = "database_cluster"
    DATABASE_SUBNET_GROUP = "database_subnet_group"
    ASSOCIATED_ROLE = "associated_role"
    DEAD_LETTER_QUEUE = "dead_letter_queue"
    RESOURCE_POLICY_PRINCIPAL = "resource_policy_principal"
    TERRAFORM_REFERENCE = "terraform_configuration_reference"
    CLOUDFORMATION_REFERENCE = "cloudformation_configuration_reference"


class TrustPrincipalKind(StrEnum):
    AWS = "aws"
    SERVICE = "service"
    FEDERATED = "federated"
    UNKNOWN = "unknown"


class RouteTargetKind(StrEnum):
    INTERNET_GATEWAY = "internet_gateway"
    NETWORK_INTERFACE = "network_interface"
    INSTANCE = "instance"
    NAT_GATEWAY = "nat_gateway"
    EGRESS_ONLY_INTERNET_GATEWAY = "egress_only_internet_gateway"
    TRANSIT_GATEWAY = "transit_gateway"
    VPC_PEERING = "vpc_peering"
    VPC_ENDPOINT = "vpc_endpoint"
    PREFIX_LIST = "prefix_list"
    LOCAL = "local"
    UNKNOWN = "unknown"


class RouteReach(StrEnum):
    """Where a selected route must lead for traffic to be allowed."""

    LOCAL = "local"
    INTERNET_GATEWAY = "internet_gateway"
    EGRESS_ONLY_INTERNET_GATEWAY = "egress_only_internet_gateway"
    NAT_GATEWAY = "nat_gateway"


class NatConnectivity(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class TransitGatewayRouteType(StrEnum):
    STATIC = "static"
    PROPAGATED = "propagated"


class VpcEndpointType(StrEnum):
    GATEWAY = "gateway"
    INTERFACE = "interface"
    GATEWAY_LOAD_BALANCER = "gateway_load_balancer"


class AddressFamily(StrEnum):
    IPV4 = "ipv4"
    IPV6 = "ipv6"


class RuleDirection(StrEnum):
    INGRESS = "ingress"
    EGRESS = "egress"


class NaclAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class LoadBalancerType(StrEnum):
    APPLICATION = "application"
    NETWORK = "network"
    GATEWAY = "gateway"


class LoadBalancerScheme(StrEnum):
    INTERNET_FACING = "internet-facing"
    INTERNAL = "internal"


class LoadBalancerProtocol(StrEnum):
    HTTP = "HTTP"
    HTTPS = "HTTPS"
    TCP = "TCP"
    TLS = "TLS"
    UDP = "UDP"
    TCP_UDP = "TCP_UDP"
    GENEVE = "GENEVE"
    QUIC = "QUIC"
    TCP_QUIC = "TCP_QUIC"


class ListenerActionType(StrEnum):
    FORWARD = "forward"
    AUTHENTICATE_OIDC = "authenticate-oidc"
    AUTHENTICATE_COGNITO = "authenticate-cognito"
    REDIRECT = "redirect"
    FIXED_RESPONSE = "fixed-response"


class TargetType(StrEnum):
    INSTANCE = "instance"
    IP = "ip"
    ALB = "alb"
    LAMBDA = "lambda"


class TargetHealthState(StrEnum):
    INITIAL = "initial"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNHEALTHY_DRAINING = "unhealthy.draining"
    UNUSED = "unused"
    DRAINING = "draining"
    UNAVAILABLE = "unavailable"


class QueryDirection(StrEnum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"
    BOTH = "both"


class QueryEdgeSelection(StrEnum):
    DEPENDENCIES = "dependencies"
    CONTAINMENT = "containment"
    BOTH = "both"


class QueryTruncationReason(StrEnum):
    DEPTH_LIMIT = "depth_limit"
    RESULT_LIMIT = "result_limit"
    WORK_LIMIT = "work_limit"


class QueryLimitationKind(StrEnum):
    COVERAGE_MISSING = "coverage_missing"
    COVERAGE_LIMITED = "coverage_limited"
    PROVENANCE_UNAVAILABLE = "provenance_unavailable"
    EXTERNAL_TARGET = "external_target"
    UNRESOLVED_TARGET = "unresolved_target"


class QueryErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    RESOURCE_NOT_FOUND = "resource_not_found"


class ImpactLabel(StrEnum):
    """Observed means recorded structure; potential means a traced downstream possibility."""

    OBSERVED = "observed"
    POTENTIAL = "potential"


class ImpactKind(StrEnum):
    DIRECT_ATTACHMENT = "direct_attachment"
    RULE_REFERENCE = "rule_reference"
    DOWNSTREAM_DEPENDENCY = "downstream_dependency"


class RuleReferenceSide(StrEnum):
    """Which group's rule holds a security-group reference."""

    SELECTED_REFERENCES_OTHER = "selected_references_other"
    OTHER_REFERENCES_SELECTED = "other_references_selected"


class TrustClassification(StrEnum):
    """Who a trust principal names, as recorded; never whether access is effective."""

    SAME_ACCOUNT = "same_account"
    EXTERNAL_ACCOUNT = "external_account"
    SERVICE = "service"
    FEDERATED = "federated"
    WILDCARD = "wildcard"
    UNRESOLVED = "unresolved"


class TrustEffect(StrEnum):
    ALLOW = "Allow"
    DENY = "Deny"


class TrustSource(StrEnum):
    ROLE_TRUST_POLICY = "role_trust_policy"
    RESOURCE_POLICY = "resource_policy"


class AccessSourceKind(StrEnum):
    """The compute resource a database-access finding starts from."""

    EC2_INSTANCE = "ec2_instance"
    ECS_TASK = "ecs_task"
    LAMBDA_FUNCTION = "lambda_function"


class AccessElement(StrEnum):
    """One composed element of a database network path, in canonical order."""

    SOURCE_EGRESS = "source_egress"
    DATABASE_INGRESS = "database_ingress"
    SOURCE_NACL = "source_nacl"
    DATABASE_NACL = "database_nacl"
    ROUTE = "route"


class ExposureEntryKind(StrEnum):
    """The kind of entry point an internet-exposure finding starts from."""

    PUBLIC_ADDRESS = "public_address"
    LOAD_BALANCER = "load_balancer"


class ExposureProtocol(StrEnum):
    TCP = "tcp"
    UDP = "udp"


class ExposureElement(StrEnum):
    """One composed element of an exposure path, in canonical order."""

    PUBLIC_ADDRESS = "public_address"
    SCHEME = "scheme"
    ROUTE = "route"
    NACL = "nacl"
    SECURITY_GROUP = "security_group"
    LISTENER = "listener"
    TARGET = "target"


class ComparisonSide(StrEnum):
    """Which of the two explicitly selected snapshots a comparison statement concerns."""

    LEFT = "left"
    RIGHT = "right"

    @property
    def opposite(self) -> "ComparisonSide":
        return ComparisonSide.RIGHT if self is ComparisonSide.LEFT else ComparisonSide.LEFT


class MatchBasis(StrEnum):
    """What paired two resources: a physical identity, or a logical address in an IaC context."""

    PHYSICAL_IDENTITY = "physical_identity"
    LOGICAL_ADDRESS = "logical_address"


class DifferenceSubject(StrEnum):
    """Which part of a matched resource a field difference concerns."""

    FIELD = "field"
    DEPENDENCY = "dependency"
    CONTAINMENT = "containment"


class DifferenceKind(StrEnum):
    """`indeterminate` marks a field that cannot be compared (a qualification says why)."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    INDETERMINATE = "indeterminate"


class QualificationKind(StrEnum):
    PLANNED_LIFECYCLE = "planned_lifecycle"
    LIMITED_COVERAGE = "limited_coverage"
    UNRECORDED_COVERAGE = "unrecorded_coverage"
    UNKNOWN_VALUE = "unknown_value"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    REDACTED_VALUE = "redacted_value"


class ComparisonLimitationKind(StrEnum):
    CROSS_CLOUD = "cross_cloud"
    SCOPE_MISMATCH = "scope_mismatch"
    SOURCE_KIND_MISMATCH = "source_kind_mismatch"
    RESULT_LIMIT_REACHED = "result_limit_reached"


class AnalyzerErrorCode(StrEnum):
    UNSUPPORTED_ANALYZER = "unsupported_analyzer"
    INVALID_PARAMETERS = "invalid_parameters"
    INVALID_RESULT = "invalid_result"
    RESOURCE_NOT_FOUND = "resource_not_found"
    UNSUPPORTED_RESOURCE = "unsupported_resource"


class NetworkDecision(StrEnum):
    """A primitive's verdict on recorded configuration, never on live reachability."""

    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


class NetworkBlockerCode(StrEnum):
    DENIED = "denied"
    MISSING_EVIDENCE = "missing_evidence"
    UNSUPPORTED_ELEMENT = "unsupported_element"
    COVERAGE_LIMITED = "coverage_limited"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
