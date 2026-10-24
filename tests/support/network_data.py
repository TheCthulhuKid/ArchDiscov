"""Shared EC2 network API fixtures and helpers for normalization tests."""

from datetime import UTC, datetime
from uuid import UUID

from archdiscov.ingestion.network import RegionResult, normalize_region
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import ArchitectureSnapshot, AwsSource, SnapshotCoverage
from archdiscov.models.enums import AwsRegion

ACCOUNT = "123456789012"
REGION = AwsRegion.EU_WEST_1
SNAPSHOT_ID = UUID(int=9)
NOW = datetime(2026, 1, 1, tzinfo=UTC)

VPC = {"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16"}
SUBNET = {"SubnetId": "subnet-1", "VpcId": "vpc-1", "CidrBlock": "10.0.1.0/24"}
SG = {
    "GroupId": "sg-1",
    "GroupName": "web",
    "VpcId": "vpc-1",
    "Description": "token=hunter2-do-not-leak",
    "IpPermissions": [
        {
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "super secret note"}],
        },
        {
            "IpProtocol": "tcp",
            "FromPort": 5432,
            "ToPort": 5432,
            "UserIdGroupPairs": [{"GroupId": "sg-2", "UserId": ACCOUNT}],
        },
    ],
    "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
}
SG_RULES = [
    {
        "SecurityGroupRuleId": "sgr-1",
        "GroupId": "sg-1",
        "IsEgress": False,
        "IpProtocol": "tcp",
        "FromPort": 443,
        "ToPort": 443,
        "CidrIpv4": "0.0.0.0/0",
        "Description": "super secret note",
    },
    {"SecurityGroupRuleId": "sgr-9", "GroupId": "sg-other", "IsEgress": True, "IpProtocol": "-1"},
]
ROUTE_TABLE = {
    "RouteTableId": "rtb-1",
    "VpcId": "vpc-1",
    "Routes": [
        {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"},
        {"DestinationCidrBlock": "10.8.0.0/16", "VpcPeeringConnectionId": "pcx-1"},
    ],
    "Associations": [{"Main": True}, {"SubnetId": "subnet-1"}],
}
ENI = {
    "NetworkInterfaceId": "eni-1",
    "SubnetId": "subnet-1",
    "VpcId": "vpc-1",
    "MacAddress": "0a:1b:2c:3d:4e:5f",
    "Association": {"PublicIp": "203.0.113.5"},
    "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.5", "Primary": True}],
    "Groups": [{"GroupId": "sg-1"}],
}
INSTANCE = {
    "InstanceId": "i-1",
    "SubnetId": "subnet-1",
    "VpcId": "vpc-1",
    "SecurityGroups": [{"GroupId": "sg-1"}, {"GroupId": "sg-gone"}],
}
ATTACHED_ENI = {**ENI, "Attachment": {"InstanceId": "i-1", "DeviceIndex": 0, "Status": "attached"}}


def region(items, unavailable=None) -> RegionResult:
    return normalize_region(SNAPSHOT_ID, ACCOUNT, REGION, items, unavailable)


def snapshot_of(result: RegionResult) -> ArchitectureSnapshot:
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT,
        collection_started_at=NOW,
        collection_completed_at=NOW,
        source=AwsSource(profile="p"),
        coverage=SnapshotCoverage(regions=[REGION], account_wide=False),
        regions={REGION: result.architecture},
        dependencies_by_region={REGION: result.dependencies} if result.dependencies else {},
        evidence=result.evidence,
    )


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


def by_id(result):
    return {r.identity.physical_id: r for r in flatten(result.architecture.resources)}


FULL = {
    Op.DESCRIBE_VPCS: [VPC],
    Op.DESCRIBE_SUBNETS: [SUBNET],
    Op.DESCRIBE_ROUTE_TABLES: [ROUTE_TABLE],
    Op.DESCRIBE_INTERNET_GATEWAYS: [
        {"InternetGatewayId": "igw-1", "Attachments": [{"VpcId": "vpc-1", "State": "available"}]}
    ],
    Op.DESCRIBE_NETWORK_ACLS: [
        {
            "NetworkAclId": "acl-1",
            "VpcId": "vpc-1",
            "Entries": [
                {
                    "RuleNumber": 100,
                    "Protocol": "6",
                    "RuleAction": "allow",
                    "Egress": False,
                    "CidrBlock": "0.0.0.0/0",
                    "PortRange": {"From": 443, "To": 443},
                }
            ],
        }
    ],
    Op.DESCRIBE_SECURITY_GROUPS: [SG],
    Op.DESCRIBE_SECURITY_GROUP_RULES: SG_RULES,
    Op.DESCRIBE_NETWORK_INTERFACES: [ENI],
}
