"""Public and private network entry points through the real EC2 normalizer."""

from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import ArchitectureSnapshot
from tests.support.nacl_data import acl as acl_item
from tests.support.network_data import SUBNET, VPC
from tests.support.network_snapshot import build_network, resource_id  # noqa: F401

PUBLIC, PRIVATE = "subnet-pub", "subnet-priv"
ALL_COVERED = {
    operation: "success"
    for operation in (
        "DescribeRouteTables",
        "DescribeInternetGateways",
        "DescribeNetworkAcls",
        "DescribeSecurityGroups",
        "DescribeSecurityGroupRules",
        "DescribeNetworkInterfaces",
        "DescribeInstances",
    )
}


def sg_rule(group, port=443, *, to_port=None, protocol="tcp", cidr="0.0.0.0/0", **fields):
    """A DescribeSecurityGroupRules ingress item; `fields` replace the CIDR (for example
    ReferencedGroupInfo or PrefixListId, with cidr=None)."""
    item = {
        "SecurityGroupRuleId": f"sgr-{group}-{protocol}-{port}-{cidr}-{len(fields)}",
        "GroupId": group,
        "IsEgress": False,
        "IpProtocol": protocol,
        "FromPort": port,
        "ToPort": to_port if to_port is not None else port,
        **fields,
    }
    if cidr is not None:
        item["CidrIpv4"] = cidr
    return item


def eni(name, subnet, groups, public=None, instance=None):
    item = {
        "NetworkInterfaceId": name,
        "SubnetId": subnet,
        "VpcId": "vpc-1",
        "Groups": [{"GroupId": g} for g in groups],
        "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.5", "Primary": True}],
    }
    if public:
        item["Association"] = {"PublicIp": public}
    if instance:
        item["Attachment"] = {"InstanceId": instance, "DeviceIndex": 0, "Status": "attached"}
    return item


def instance(name, subnet, groups, public=None):
    item = {
        "InstanceId": name,
        "SubnetId": subnet,
        "VpcId": "vpc-1",
        "SecurityGroups": [{"GroupId": g} for g in groups],
    }
    if public:
        item["PublicIpAddress"] = public
    return item


DEFAULT_RULES = [
    sg_rule("sg-web", 443),
    sg_rule("sg-web", 80, to_port=90),
    sg_rule("sg-web", 22, cidr="10.0.0.0/8"),
    sg_rule("sg-closed", 22, cidr="10.0.0.0/8"),
]
OPEN_ACL = [
    {
        "RuleNumber": 100,
        "Protocol": "-1",
        "RuleAction": "allow",
        "Egress": False,
        "CidrBlock": "0.0.0.0/0",
    },
    {
        "RuleNumber": 100,
        "Protocol": "-1",
        "RuleAction": "allow",
        "Egress": True,
        "CidrBlock": "0.0.0.0/0",
    },
]


def items(*, rules=None, acl=None, enis=None, instances=None, tables=True, acls=True):
    """A public subnet (internet-gateway route), a private subnet (NAT route), and entries."""
    data = {
        Op.DESCRIBE_VPCS: [VPC],
        Op.DESCRIBE_SUBNETS: [{**SUBNET, "SubnetId": PUBLIC}, {**SUBNET, "SubnetId": PRIVATE}],
        Op.DESCRIBE_INTERNET_GATEWAYS: [
            {
                "InternetGatewayId": "igw-1",
                "Attachments": [{"VpcId": "vpc-1", "State": "available"}],
            }
        ],
        Op.DESCRIBE_SECURITY_GROUPS: [
            {"GroupId": g, "VpcId": "vpc-1", "IpPermissions": [], "IpPermissionsEgress": []}
            for g in ("sg-web", "sg-closed")
        ],
        Op.DESCRIBE_SECURITY_GROUP_RULES: DEFAULT_RULES if rules is None else rules,
        Op.DESCRIBE_NETWORK_INTERFACES: [
            eni("eni-web", PUBLIC, ["sg-web"], public="198.51.100.10"),
            eni("eni-closed", PUBLIC, ["sg-closed"], public="198.51.100.11"),
            eni("eni-private", PUBLIC, ["sg-web"]),
        ]
        if enis is None
        else enis,
        Op.DESCRIBE_INSTANCES: [
            {
                "Instances": [
                    instance("i-public", PUBLIC, ["sg-web"], public="198.51.100.20"),
                    instance("i-private", PUBLIC, ["sg-web"]),
                ]
            }
        ]
        if instances is None
        else instances,
    }
    if tables:
        data[Op.DESCRIBE_ROUTE_TABLES] = [
            {
                "RouteTableId": "rtb-pub",
                "VpcId": "vpc-1",
                "Routes": [
                    {
                        "DestinationCidrBlock": "10.0.0.0/16",
                        "GatewayId": "local",
                        "State": "active",
                    },
                    {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"},
                ],
                "Associations": [{"SubnetId": PUBLIC}],
            },
            {
                "RouteTableId": "rtb-priv",
                "VpcId": "vpc-1",
                "Routes": [
                    {
                        "DestinationCidrBlock": "10.0.0.0/16",
                        "GatewayId": "local",
                        "State": "active",
                    },
                    {
                        "DestinationCidrBlock": "0.0.0.0/0",
                        "NatGatewayId": "nat-1",
                        "State": "active",
                    },
                ],
                "Associations": [{"SubnetId": PRIVATE}],
            },
        ]
    if acls:
        data[Op.DESCRIBE_NETWORK_ACLS] = [
            acl_item("acl-1", OPEN_ACL if acl is None else acl, PUBLIC, PRIVATE)
        ]
    return data


def build(*, covered=None, **options) -> ArchitectureSnapshot:
    return build_network(
        items(**options), subnets=(PUBLIC, PRIVATE), covered={**ALL_COVERED, **(covered or {})}
    )
