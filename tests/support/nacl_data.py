"""Snapshots of subnets with associated, cited network ACLs for NACL evaluation tests."""

from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import ArchitectureSnapshot
from tests.support.network_snapshot import build_network

ALL = {"RuleNumber": 32767, "Protocol": "-1", "RuleAction": "deny", "CidrBlock": "0.0.0.0/0"}


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
    return build_network(
        {Op.DESCRIBE_NETWORK_ACLS: acls},
        subnets=subnets,
        covered={"DescribeNetworkAcls": status},
    )
