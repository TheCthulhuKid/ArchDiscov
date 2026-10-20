"""A public subnet with a load balancer in front of a private backend, through the real pipeline."""

from uuid import UUID

from archdiscov.ingestion.elbv2.relationships import merge_resources, resolve_relationships
from archdiscov.ingestion.network import normalize_region
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    CollectionFailure,
    RegionArchitecture,
    SnapshotCoverage,
)
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from tests.support.coverage_data import coverage_record
from tests.support.elbv2_data import ACCOUNT, GROUP, LB, LISTENER, RULE, responses
from tests.support.elbv2_snapshot import normalized
from tests.support.ids import stable_id

REGION = AwsRegion.US_EAST_1
SNAPSHOT_ID = UUID(int=15)
__all__ = [
    "FULL_COVERAGE",
    "GROUP",
    "LB",
    "LISTENER",
    "RULE",
    "build",
    "ec2_items",
    "rid",
    "responses",
]


def rid(physical_id, service=AwsService.EC2):
    return stable_id(physical_id, REGION, service)


ADMITS_LOAD_BALANCER = {"GroupId": "sg-a", "UserId": ACCOUNT}


def _rule(group, port, **target):
    item = {
        "SecurityGroupRuleId": f"sgr-{group}-{port}",
        "GroupId": group,
        "IsEgress": False,
        "IpProtocol": "tcp",
        "FromPort": port,
        "ToPort": port,
        **target,
    }
    return item


def ec2_items(*, backend_rules=None, public_route=True, acl=True, public_eni=False):
    """vpc-a with a public subnet (subnet-a), a private one (subnet-b), and the backend."""
    routes = [{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"}]
    if public_route:
        routes.append(
            {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}
        )
    items = {
        Op.DESCRIBE_VPCS: [{"VpcId": "vpc-a", "CidrBlock": "10.0.0.0/16"}],
        Op.DESCRIBE_SUBNETS: [
            {"SubnetId": "subnet-a", "VpcId": "vpc-a", "CidrBlock": "10.0.1.0/24"},
            {"SubnetId": "subnet-b", "VpcId": "vpc-a", "CidrBlock": "10.0.2.0/24"},
        ],
        Op.DESCRIBE_INTERNET_GATEWAYS: [
            {
                "InternetGatewayId": "igw-1",
                "Attachments": [{"VpcId": "vpc-a", "State": "available"}],
            }
        ],
        Op.DESCRIBE_ROUTE_TABLES: [
            {
                "RouteTableId": "rtb-1",
                "VpcId": "vpc-a",
                "Routes": routes,
                "Associations": [{"SubnetId": "subnet-a"}, {"SubnetId": "subnet-b"}],
            }
        ],
        Op.DESCRIBE_SECURITY_GROUPS: [
            {"GroupId": g, "VpcId": "vpc-a", "IpPermissions": [], "IpPermissionsEgress": []}
            for g in ("sg-a", "sg-backend")
        ],
        Op.DESCRIBE_SECURITY_GROUP_RULES: [
            _rule("sg-a", 80, CidrIpv4="0.0.0.0/0"),
            *(
                [
                    _rule(
                        "sg-backend",
                        8080,
                        ReferencedGroupInfo={"GroupId": "sg-a", "UserId": ACCOUNT},
                    )
                ]
                if backend_rules is None
                else backend_rules
            ),
        ],
        Op.DESCRIBE_INSTANCES: [
            {
                "Instances": [
                    {
                        "InstanceId": "i-web",
                        "SubnetId": "subnet-b",
                        "VpcId": "vpc-a",
                        "SecurityGroups": [{"GroupId": "sg-backend"}],
                    }
                ]
            }
        ],
    }
    if public_eni:
        items[Op.DESCRIBE_NETWORK_INTERFACES] = [
            {
                "NetworkInterfaceId": "eni-public",
                "SubnetId": "subnet-a",
                "VpcId": "vpc-a",
                "Groups": [{"GroupId": "sg-a"}],
                "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.7", "Primary": True}],
                "Association": {"PublicIp": "198.51.100.7"},
            }
        ]
    if acl:
        items[Op.DESCRIBE_NETWORK_ACLS] = [
            {
                "NetworkAclId": "acl-1",
                "VpcId": "vpc-a",
                "Entries": [
                    {
                        "RuleNumber": 100,
                        "Protocol": "-1",
                        "RuleAction": "allow",
                        "Egress": egress,
                        "CidrBlock": "0.0.0.0/0",
                    }
                    for egress in (False, True)
                ],
                "Associations": [{"SubnetId": "subnet-a"}, {"SubnetId": "subnet-b"}],
            }
        ]
    return items


def build(data=None, *, ec2=None, limited=None, coverage=None) -> ArchitectureSnapshot:
    """An internet-facing application balancer in subnet-a forwarding to instance i-web.

    `coverage` maps (service, operation) to a status recorded in addition to the ELBv2 records.
    `data` is the ELBv2 API data (default: `responses()` made internet-facing); `limited`
    names an ELBv2 or EC2 operation whose recorded coverage is failed.
    """
    if data is None:
        data = responses()
        data["describe_load_balancers"]["LoadBalancers"][0]["Scheme"] = "internet-facing"
    elb = normalized(data)
    region = normalize_region(SNAPSHOT_ID, ACCOUNT, REGION, ec2 or ec2_items())
    roots = region.architecture.resources
    merge_resources(roots, elb.resources)
    links = [*region.dependencies, *resolve_relationships(roots, elb.evidence)]
    operations = list(elb.coverage)
    if limited is not None:
        operations = [
            record.model_copy(
                update={
                    "status": CoverageStatus.FAILED,
                    "result_count": None,
                    "failures": [CollectionFailure(code="AccessDenied", message="Denied")],
                }
            )
            if record.operation == limited
            else record
            for record in operations
        ]
    at = operations[0].started_at
    operations += [
        coverage_record(service, operation, status, 1, REGION, at)
        for (service, operation), status in (coverage or {}).items()
    ]
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT,
        collection_started_at=min(item.started_at for item in operations),
        collection_completed_at=max(item.completed_at for item in operations),
        source={"kind": "aws"},
        regions={REGION: RegionArchitecture(resources=roots)},
        coverage=SnapshotCoverage(regions=[REGION], account_wide=False, operations=operations),
        dependencies_by_region={REGION: links},
        evidence={**region.evidence, **elb.evidence},
    )


FULL_COVERAGE = {
    (AwsService.EC2, operation): "success"
    for operation in (
        "DescribeInstances",
        "DescribeNetworkInterfaces",
        "DescribeNetworkAcls",
        "DescribeRouteTables",
        "DescribeInternetGateways",
        "DescribeSecurityGroups",
        "DescribeSecurityGroupRules",
    )
}
