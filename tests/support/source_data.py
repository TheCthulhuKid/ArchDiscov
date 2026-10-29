"""A database VPC with EC2, ECS, and Lambda sources, through the real normalizers."""

from archdiscov.ingestion.configuration_relationships import merge_resources, resolve_relationships
from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.ingestion.network import normalize_region
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    RegionArchitecture,
    ResourceIdentity,
    SnapshotCoverage,
)
from archdiscov.models.enums import AwsService
from archdiscov.models.resources.ecs import EcsTask, EcsTaskDefinition
from archdiscov.models.resources.functions import LambdaFunction
from tests.support.coverage_data import coverage_record
from tests.support.ids import stable_id
from tests.support.rds_data import ACCOUNT, REGION, SNAPSHOT, collect, data

VPC_A = {"VpcId": "vpc-a", "CidrBlock": "10.0.0.0/16"}
VPC_B = {"VpcId": "vpc-b", "CidrBlock": "10.9.0.0/16"}
SUBNETS = [
    {"SubnetId": "subnet-a", "VpcId": "vpc-a", "CidrBlock": "10.0.1.0/24"},
    {"SubnetId": "subnet-b", "VpcId": "vpc-a", "CidrBlock": "10.0.2.0/24"},
    {"SubnetId": "subnet-x", "VpcId": "vpc-b", "CidrBlock": "10.9.1.0/24"},
]
GROUPS = [
    {"GroupId": gid, "VpcId": vpc, "IpPermissions": [], "IpPermissionsEgress": []}
    for gid, vpc in (("sg-a", "vpc-a"), ("sg-app", "vpc-a"), ("sg-x", "vpc-b"))
]
TASK = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/app/0123456789abcdef"
FUNCTION = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:worker"
DEFINITION = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/app:1"


def eni(name, subnet, groups, address, instance=None):
    item = {
        "NetworkInterfaceId": name,
        "SubnetId": subnet,
        "VpcId": "vpc-a",
        "Groups": [{"GroupId": g} for g in groups],
        "PrivateIpAddresses": [{"PrivateIpAddress": address, "Primary": True}],
    }
    if instance:
        item["Attachment"] = {"InstanceId": instance, "DeviceIndex": 0, "Status": "attached"}
    return item


def instance(name, subnet, groups, vpc="vpc-a", address=None):
    item = {
        "InstanceId": name,
        "SubnetId": subnet,
        "VpcId": vpc,
        "SecurityGroups": [{"GroupId": g} for g in groups],
    }
    if address:
        item["PrivateIpAddress"] = address
    return item


def rule(group, *, egress=False, protocol="tcp", port=5432, **target):
    """A DescribeSecurityGroupRules item; `target` is a CIDR or a referenced group."""
    item = {
        "SecurityGroupRuleId": f"sgr-{group}-{len(target)}-{port}-{egress}",
        "GroupId": group,
        "IsEgress": egress,
        "IpProtocol": protocol,
        "FromPort": port if protocol != "-1" else -1,
        "ToPort": port if protocol != "-1" else -1,
    }
    if "referenced" in target:
        item["ReferencedGroupInfo"] = {"GroupId": target["referenced"], "UserId": ACCOUNT}
    else:
        item["CidrIpv4"] = target["cidr"]
    return item


def acl_entry(number, action="allow", *, egress=False, protocol="-1", cidr="0.0.0.0/0"):
    return {
        "RuleNumber": number,
        "Protocol": protocol,
        "RuleAction": action,
        "Egress": egress,
        "CidrBlock": cidr,
    }


OPEN_ACL = [acl_entry(100), acl_entry(100, egress=True)]
DEFAULT_RULES = [
    rule("sg-a", referenced="sg-app"),  # the database accepts the application group
    rule("sg-app", egress=True, protocol="-1", cidr="0.0.0.0/0"),
    rule("sg-a", egress=True, protocol="-1", cidr="0.0.0.0/0"),
]
LOCAL_ROUTE = {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"}


def ec2_items(*, rules=None, acl=None, routes=None, acls=True, tables=True):
    """The EC2 network: rules, one ACL for both subnets, and one local-route table.

    `acls=False` and `tables=False` record no ACL or route table at all.
    """
    items = {
        Op.DESCRIBE_VPCS: [VPC_A, VPC_B],
        Op.DESCRIBE_SUBNETS: SUBNETS,
        Op.DESCRIBE_SECURITY_GROUPS: GROUPS,
        Op.DESCRIBE_SECURITY_GROUP_RULES: DEFAULT_RULES if rules is None else rules,
        Op.DESCRIBE_INSTANCES: [
            {
                "Instances": [
                    instance("i-multi", "subnet-a", ["sg-app"]),
                    instance("i-plain", "subnet-a", ["sg-app"], address="10.0.1.20"),
                    instance("i-nosg", "subnet-a", []),
                    instance("i-other", "subnet-x", ["sg-x"], vpc="vpc-b", address="10.9.1.5"),
                ]
            }
        ],
        Op.DESCRIBE_NETWORK_INTERFACES: [
            eni("eni-1", "subnet-a", ["sg-app"], "10.0.1.10", "i-multi"),
            eni("eni-2", "subnet-b", ["sg-app"], "10.0.2.10", "i-multi"),
            eni("eni-task", "subnet-a", ["sg-app"], "10.0.1.30"),
        ],
    }
    if acls:
        items[Op.DESCRIBE_NETWORK_ACLS] = [
            {
                "NetworkAclId": "acl-1",
                "VpcId": "vpc-a",
                "Entries": OPEN_ACL if acl is None else acl,
                "Associations": [{"SubnetId": s} for s in ("subnet-a", "subnet-b")],
            }
        ]
    if tables:
        items[Op.DESCRIBE_ROUTE_TABLES] = [
            {
                "RouteTableId": "rtb-1",
                "VpcId": "vpc-a",
                "Routes": [LOCAL_ROUTE] if routes is None else routes,
                "Associations": [{"SubnetId": s} for s in ("subnet-a", "subnet-b")],
            }
        ]
    return items


EC2_ITEMS = ec2_items()


def _ecs_task(builder, *, subnets=("subnet-a",), groups=("sg-app",), mode="awsvpc"):
    enis = ("eni-task",)
    identity = ResourceIdentity(
        account_id=ACCOUNT, region=REGION, service=AwsService.ECS, physical_id=TASK
    )
    task = EcsTask(
        identity=identity,
        arn=TASK,
        task_definition_arn=DEFINITION,
        subnet_ids=list(subnets) or None,
        security_group_ids=list(groups) or None,
        network_interface_ids=list(enis),
    )
    for pointer, values in (
        ("subnet_ids", subnets),
        ("security_group_ids", groups),
        ("network_interface_ids", enis),
    ):
        for position, value in enumerate(values):
            builder.cite(task, f"/{pointer}/{position}", "DescribeTasks", f"Task.{pointer}", value)
    builder.cite(
        task, "/task_definition_arn", "DescribeTasks", "Task.TaskDefinitionArn", DEFINITION
    )
    definition = EcsTaskDefinition(
        identity=ResourceIdentity(
            account_id=ACCOUNT, region=REGION, service=AwsService.ECS, physical_id=DEFINITION
        ),
        arn=DEFINITION,
        network_mode=mode,
    )
    return [task, definition]


def _lambda(builder, *, vpc="vpc-a", subnets=("subnet-b",), groups=("sg-app",)):
    function = LambdaFunction(
        identity=ResourceIdentity(
            account_id=ACCOUNT, region=REGION, service=AwsService.LAMBDA, physical_id=FUNCTION
        ),
        arn=FUNCTION,
        vpc_id=vpc,
        subnet_ids=list(subnets),
        security_group_ids=list(groups),
    )
    for pointer, values in (("subnet_ids", subnets), ("security_group_ids", groups)):
        for position, value in enumerate(values):
            builder.cite(
                function, f"/{pointer}/{position}", "GetFunction", f"VpcConfig.{pointer}", value
            )
    return [function]


def build(
    *, ec2=None, task=None, function=None, rds=None, coverage=None, rds_status=None
) -> ArchitectureSnapshot:
    """The EC2 network, an RDS database, and optional ECS task and Lambda function.

    `task` and `function` are keyword dictionaries for the fakes (None: omit the source).
    `coverage` maps (service, operation) to a status and is recorded in addition to RDS's own;
    `rds_status` replaces the status of the RDS collection records.
    """
    result = normalize_region(SNAPSHOT, ACCOUNT, REGION, ec2 or EC2_ITEMS)
    roots = result.architecture.resources
    database = collect(rds or data())
    builder = EvidenceBuilder(SNAPSHOT, ACCOUNT, REGION, AwsService.ECS)
    config = list(database.resources)
    if task is not None:
        config += _ecs_task(builder, **task)
    if function is not None:
        config += _lambda(builder, **function)
    merge_resources(roots, config)
    evidence = {**result.evidence, **database.evidence, **builder.evidence}
    links = [*result.dependencies, *resolve_relationships(roots, evidence).get(REGION, [])]
    at = database.coverage[0].started_at
    records = [
        coverage_record(service, operation, status, 1, REGION, at)
        for (service, operation), status in (coverage or {}).items()
    ]
    rds_records = database.coverage
    if rds_status:
        rds_records = [
            coverage_record(AwsService.RDS, record.operation, rds_status, 1, REGION, at)
            for record in database.coverage
        ]
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT,
        account_id=ACCOUNT,
        collection_started_at=database.coverage[0].started_at,
        collection_completed_at=database.coverage[0].completed_at,
        source={"kind": "aws"},
        regions={REGION: RegionArchitecture(resources=roots)},
        coverage=SnapshotCoverage(
            regions=[REGION], account_wide=False, operations=[*rds_records, *records]
        ),
        dependencies_by_region={REGION: links},
        evidence=evidence,
    )


def rid(physical_id, service=AwsService.EC2):
    return stable_id(physical_id, REGION, service)


FULL_COVERAGE = {
    (AwsService.EC2, "DescribeInstances"): "success",
    (AwsService.EC2, "DescribeNetworkInterfaces"): "success",
    (AwsService.EC2, "DescribeNetworkAcls"): "success",
    (AwsService.EC2, "DescribeRouteTables"): "success",
    (AwsService.EC2, "DescribeSecurityGroups"): "success",
    (AwsService.ECS, "DescribeTasks"): "success",
    (AwsService.LAMBDA, "GetFunctionConfiguration"): "success",
}
