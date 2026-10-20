"""Snapshots of subnets with associated, cited network ACLs for NACL evaluation tests."""

from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    AwsSource,
    SnapshotCoverage,
)
from archdiscov.models.enums import AwsService
from archdiscov.models.resource_base import ResourceIdentity
from tests.support.coverage_data import REGION, ec2_coverage
from tests.support.iam_data import ACCOUNT, context
from tests.support.network_data import SNAPSHOT_ID, SUBNET, VPC, region

ALL = {"RuleNumber": 32767, "Protocol": "-1", "RuleAction": "deny", "CidrBlock": "0.0.0.0/0"}


def resource_id(physical_id: str) -> str:
    return ResourceIdentity(
        account_id=ACCOUNT, region=REGION, service=AwsService.EC2, physical_id=physical_id
    ).stable_id


def entry(number, action="allow", *, egress=False, protocol="6", ports=(443, 443), **fields):
    """An API-shaped ACL entry; `ports` None records no port range."""
    data = {
        "RuleNumber": number,
        "Protocol": protocol,
        "RuleAction": action,
        "Egress": egress,
        "CidrBlock": "0.0.0.0/0",
        **fields,
    }
    if ports is not None:
        data["PortRange"] = {"From": ports[0], "To": ports[1]}
    return data


def acl(name: str, entries: list[dict], *subnets: str) -> dict:
    return {
        "NetworkAclId": name,
        "VpcId": "vpc-1",
        "Entries": entries,
        "Associations": [
            {"SubnetId": s, "NetworkAclAssociationId": f"aclassoc-{s}"} for s in subnets
        ],
    }


def build(
    acls: list[dict],
    *,
    subnets: tuple[str, ...] = ("subnet-1",),
    status: str | None = "success",
) -> ArchitectureSnapshot:
    """ACLs through the real normalizer, so entry citations and association edges are genuine.

    A `status` of None records no EC2 coverage.
    """
    result = region(
        {
            Op.DESCRIBE_VPCS: [VPC],
            Op.DESCRIBE_SUBNETS: [{**SUBNET, "SubnetId": s} for s in subnets],
            Op.DESCRIBE_NETWORK_ACLS: acls,
        }
    )
    operations = [ec2_coverage("DescribeNetworkAcls", status, len(acls))] if status else []
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT,
        collection_started_at=context(None).clock(),
        collection_completed_at=context(None).clock(),
        source=AwsSource(profile="test"),
        regions={REGION: result.architecture},
        coverage=SnapshotCoverage(regions=[REGION], account_wide=False, operations=operations),
        dependencies_by_region={REGION: result.dependencies} if result.dependencies else {},
        evidence=result.evidence,
    )
