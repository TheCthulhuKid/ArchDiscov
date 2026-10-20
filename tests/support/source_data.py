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


EC2_ITEMS = {
    Op.DESCRIBE_VPCS: [VPC_A, VPC_B],
    Op.DESCRIBE_SUBNETS: SUBNETS,
    Op.DESCRIBE_SECURITY_GROUPS: GROUPS,
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


def _ecs_task(
    builder, *, subnets=("subnet-a",), groups=("sg-app",), enis=("eni-task",), mode="awsvpc"
):
    identity = ResourceIdentity(
        account_id=ACCOUNT, region=REGION, service=AwsService.ECS, physical_id=TASK
    )
    task = EcsTask(
        identity=identity,
        arn=TASK,
        task_definition_arn=DEFINITION,
        subnet_ids=list(subnets) or None,
        security_group_ids=list(groups) or None,
        network_interface_ids=list(enis) or None,
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


def build(*, ec2=None, task=None, function=None, rds=None) -> ArchitectureSnapshot:
    """The EC2 network, an RDS database, and optional ECS task and Lambda function.

    `task` and `function` are keyword dictionaries for the fakes (None: omit the source).
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
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT,
        account_id=ACCOUNT,
        collection_started_at=database.coverage[0].started_at,
        collection_completed_at=database.coverage[0].completed_at,
        source={"kind": "aws"},
        regions={REGION: RegionArchitecture(resources=roots)},
        coverage=SnapshotCoverage(
            regions=[REGION], account_wide=False, operations=database.coverage
        ),
        dependencies_by_region={REGION: links},
        evidence=evidence,
    )


def rid(physical_id, service=AwsService.EC2):
    return stable_id(physical_id, REGION, service)
