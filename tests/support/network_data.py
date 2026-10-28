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
NAT_GATEWAY = {
    "NatGatewayId": "nat-1",
    "VpcId": "vpc-1",
    "SubnetId": "subnet-1",
    "State": "available",
    "ConnectivityType": "public",
    "NatGatewayAddresses": [
        {
            "AllocationId": "eipalloc-1",
            "NetworkInterfaceId": "eni-nat",
            "PrivateIp": "10.0.1.9",
            "PublicIp": "198.51.100.20",
            "IsPrimary": True,
        }
    ],
}
EGRESS_ONLY_GATEWAY = {
    "EgressOnlyInternetGatewayId": "eigw-1",
    "Attachments": [{"State": "attached", "VpcId": "vpc-1"}],
}
PEERING = {
    "VpcPeeringConnectionId": "pcx-1",
    "Status": {"Code": "active", "Message": "Active"},
    "RequesterVpcInfo": {
        "VpcId": "vpc-1",
        "OwnerId": ACCOUNT,
        "CidrBlock": "10.0.0.0/16",
        "Region": "eu-west-1",
    },
    "AccepterVpcInfo": {
        "VpcId": "vpc-2",
        "OwnerId": ACCOUNT,
        "CidrBlockSet": [{"CidrBlock": "10.8.0.0/16"}],
        "Region": "eu-west-1",
    },
}
GATEWAY_ENDPOINT = {
    "VpcEndpointId": "vpce-1",
    "VpcEndpointType": "Gateway",
    "ServiceName": "com.amazonaws.eu-west-1.s3",
    "State": "available",
    "VpcId": "vpc-1",
    "OwnerId": ACCOUNT,
    "RouteTableIds": ["rtb-1"],
}
INTERFACE_ENDPOINT = {
    "VpcEndpointId": "vpce-2",
    "VpcEndpointType": "Interface",
    "ServiceName": "com.amazonaws.eu-west-1.sqs",
    "State": "available",
    "VpcId": "vpc-1",
    "OwnerId": ACCOUNT,
    "SubnetIds": ["subnet-1"],
    "NetworkInterfaceIds": ["eni-1"],
    "Groups": [{"GroupId": "sg-1", "GroupName": "web"}],
}
PREFIX_LISTS = [
    {
        "PrefixListId": "pl-s3",
        "AddressFamily": "IPv4",
        "State": "create-complete",
        "PrefixListName": "com.amazonaws.eu-west-1.s3",
        "OwnerId": "AWS",
    },
    {
        "PrefixListId": "pl-office",
        "AddressFamily": "IPv4",
        "State": "modify-complete",
        "PrefixListName": "office",
        "OwnerId": ACCOUNT,
        "Version": 3,
    },
]
PREFIX_LIST_ENTRIES = {
    "pl-s3": {"Entries": [{"Cidr": "52.218.0.0/17"}]},
    "pl-office": {"Entries": [{"Cidr": "198.51.100.0/24", "Description": "office"}]},
}
TRANSIT_GATEWAY = {
    "TransitGatewayId": "tgw-1",
    "State": "available",
    "OwnerId": ACCOUNT,
    "Options": {
        "AssociationDefaultRouteTableId": "tgw-rtb-1",
        "PropagationDefaultRouteTableId": "tgw-rtb-1",
    },
}
# Shared through AWS RAM: listed in this account, owned by another.
SHARED_TRANSIT_GATEWAY = {
    "TransitGatewayId": "tgw-shared",
    "State": "available",
    "OwnerId": "210987654321",
}
TRANSIT_GATEWAY_ATTACHMENTS = [
    {
        "TransitGatewayAttachmentId": "tgw-attach-1",
        "TransitGatewayId": "tgw-1",
        "TransitGatewayOwnerId": ACCOUNT,
        "ResourceType": "vpc",
        "ResourceId": "vpc-1",
        "ResourceOwnerId": ACCOUNT,
        "State": "available",
        "Association": {"TransitGatewayRouteTableId": "tgw-rtb-1", "State": "associated"},
    },
    {
        "TransitGatewayAttachmentId": "tgw-attach-2",
        "TransitGatewayId": "tgw-shared",
        "TransitGatewayOwnerId": "210987654321",
        "ResourceType": "vpc",
        "ResourceId": "vpc-1",
        "ResourceOwnerId": ACCOUNT,
        "State": "available",
    },
]
# The VPC attachment read: the same attachment, with the subnets it serves.
TRANSIT_GATEWAY_VPC_ATTACHMENT = {
    "TransitGatewayAttachmentId": "tgw-attach-1",
    "TransitGatewayId": "tgw-1",
    "VpcId": "vpc-1",
    "VpcOwnerId": ACCOUNT,
    "State": "available",
    "SubnetIds": ["subnet-1"],
}
TRANSIT_GATEWAY_ROUTE_TABLE = {
    "TransitGatewayRouteTableId": "tgw-rtb-1",
    "TransitGatewayId": "tgw-1",
    "State": "available",
}
ATTACHED_VPC = {"ResourceId": "vpc-1", "ResourceType": "vpc"}
# Each route table detail read's single page, by client method.
TRANSIT_GATEWAY_TABLE_PAGES = {
    "get_transit_gateway_route_table_associations": {
        "Associations": [
            {"TransitGatewayAttachmentId": "tgw-attach-1", **ATTACHED_VPC, "State": "associated"}
        ]
    },
    "get_transit_gateway_route_table_propagations": {
        "TransitGatewayRouteTablePropagations": [
            {"TransitGatewayAttachmentId": "tgw-attach-1", **ATTACHED_VPC, "State": "enabled"}
        ]
    },
    "search_transit_gateway_routes": {
        "Routes": [
            {
                "DestinationCidrBlock": "10.0.0.0/16",
                "TransitGatewayAttachments": [
                    {"TransitGatewayAttachmentId": "tgw-attach-1", **ATTACHED_VPC}
                ],
                "Type": "propagated",
                "State": "active",
            },
            {"DestinationCidrBlock": "192.168.0.0/16", "Type": "static", "State": "blackhole"},
        ],
        "AdditionalRoutesAvailable": False,
    },
}
PRIVATE_ROUTE_TABLE = {
    "RouteTableId": "rtb-private",
    "VpcId": "vpc-1",
    "Routes": [
        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"},
        {
            "DestinationIpv6CidrBlock": "::/0",
            "EgressOnlyInternetGatewayId": "eigw-1",
            "State": "active",
        },
    ],
}


def element_pages() -> dict:
    """Fake-client pages for every element read beyond FULL: NAT and egress-only gateways,
    peering, endpoints, prefix lists, and transit gateways, with their detail reads."""
    return {
        "describe_nat_gateways": [{"NatGateways": [NAT_GATEWAY]}],
        "describe_egress_only_internet_gateways": [
            {"EgressOnlyInternetGateways": [EGRESS_ONLY_GATEWAY]}
        ],
        "describe_vpc_peering_connections": [{"VpcPeeringConnections": [PEERING]}],
        "describe_vpc_endpoints": [{"VpcEndpoints": [GATEWAY_ENDPOINT, INTERFACE_ENDPOINT]}],
        "describe_managed_prefix_lists": [{"PrefixLists": PREFIX_LISTS}],
        "get_managed_prefix_list_entries": lambda PrefixListId, **_: PREFIX_LIST_ENTRIES[
            PrefixListId
        ],
        "describe_transit_gateways": [{"TransitGateways": [TRANSIT_GATEWAY]}],
        "describe_transit_gateway_attachments": [
            {"TransitGatewayAttachments": TRANSIT_GATEWAY_ATTACHMENTS[:1]}
        ],
        "describe_transit_gateway_vpc_attachments": [
            {"TransitGatewayVpcAttachments": [TRANSIT_GATEWAY_VPC_ATTACHMENT]}
        ],
        "describe_transit_gateway_route_tables": [
            {"TransitGatewayRouteTables": [TRANSIT_GATEWAY_ROUTE_TABLE]}
        ],
        **{
            method: (lambda page=page, **_: page)
            for method, page in TRANSIT_GATEWAY_TABLE_PAGES.items()
        },
    }


def region(items, unavailable=None, **details) -> RegionResult:
    return normalize_region(SNAPSHOT_ID, ACCOUNT, REGION, items, unavailable, **details)


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
