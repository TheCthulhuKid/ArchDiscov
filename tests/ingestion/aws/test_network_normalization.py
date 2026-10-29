from archdiscov.ingestion.network.resources import FALLBACK_LIMITATION
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.enums import ResourceType, RouteTargetKind, RuleDirection
from tests.support.network_data import (
    ENI,
    FULL,
    NAT_GATEWAY,
    SG,
    SUBNET,
    VPC,
    by_id,
    flatten,
    region,
    snapshot_of,
)


def _ipv6_block(cidr: str, state: str = "associated") -> dict:
    return {"Ipv6CidrBlock": cidr, "Ipv6CidrBlockState": {"State": state}}


def test_vpc_resources_nest_cite_their_entries_and_redact_descriptions():
    tagged = {**SG, "Tags": [{"Key": "password", "Value": "tag-secret"}]}
    result = region({**FULL, Op.DESCRIBE_SECURITY_GROUPS: [tagged]})
    (vpc,) = result.architecture.resources
    kinds = {c.resource_type for c in vpc.children}
    assert kinds == {
        ResourceType.SUBNET,
        ResourceType.ROUTE_TABLE,
        ResourceType.INTERNET_GATEWAY,
        ResourceType.NETWORK_ACL,
        ResourceType.SECURITY_GROUP,
    }
    (subnet,) = (c for c in vpc.children if c.resource_type is ResourceType.SUBNET)
    assert [c.identity.physical_id for c in subnet.children] == ["eni-1"]
    assert subnet.children[0].ipv6_addresses is None  # the response had no IPv6 field

    table = by_id(result)["rtb-1"]
    assert [r.target_kind for r in table.routes] == [
        RouteTargetKind.LOCAL,
        RouteTargetKind.INTERNET_GATEWAY,
        RouteTargetKind.VPC_PEERING,
    ]
    cited = result.evidence[table.fact_evidence["/routes/1"][0]]
    assert cited.configuration_path == "RouteTables[0].Routes[1]"
    assert cited.locator == "ec2:DescribeRouteTables"
    assert cited.value["GatewayId"] == "igw-1"
    assert table.attributes["is_main"] is True

    group = by_id(result)["sg-1"]
    assert [r.rule_id for r in group.ingress_rules] == ["sgr-1"]
    assert group.egress_rules == []  # sgr-9 belongs to another group
    assert group.attributes["rules_source"] == "DescribeSecurityGroupRules"
    assert "limitations" not in group.attributes
    cited = result.evidence[group.fact_evidence["/ingress_rules/0"][0]]
    assert cited.locator == "ec2:DescribeSecurityGroupRules"
    assert cited.configuration_path == "SecurityGroupRules[0]"

    assert "/attributes/tags" in group.fact_evidence

    # Free-text descriptions and secret-named tag values are redacted from every evidence record.
    text = snapshot_of(result).model_dump_json()  # integrity: citations and identities resolve
    assert "hunter2" not in text
    assert "super secret note" not in text
    assert "tag-secret" not in text
    assert result.evidence[group.evidence_ids[0]].redacted_paths == [
        "/Description",
        "/IpPermissions/0/IpRanges/0/Description",
        "/Tags/0/Value",
    ]


def test_dual_stack_fields_are_read_and_cited_to_their_entries():
    vpc = {
        **VPC,
        "Ipv6CidrBlockAssociationSet": [
            _ipv6_block("2001:db8:9::/56", "disassociated"),
            _ipv6_block("2001:db8:1::/56"),
        ],
    }
    subnet = {**SUBNET, "Ipv6CidrBlockAssociationSet": [_ipv6_block("2001:db8:1:1::/64")]}
    eni = {**ENI, "Ipv6Addresses": [{"Ipv6Address": "2001:db8:1:1::10"}]}
    table = {
        "RouteTableId": "rtb-6",
        "VpcId": "vpc-1",
        "Routes": [
            {"DestinationIpv6CidrBlock": "::/0", "EgressOnlyInternetGatewayId": "eigw-1"},
            {"DestinationPrefixListId": "pl-s3", "GatewayId": "vpce-1"},
        ],
    }
    result = region(
        {
            Op.DESCRIBE_VPCS: [vpc],
            Op.DESCRIBE_SUBNETS: [subnet],
            Op.DESCRIBE_NETWORK_INTERFACES: [eni],
            Op.DESCRIBE_ROUTE_TABLES: [table],
        }
    )
    resources = by_id(result)

    def cited(physical_id: str, pointer: str) -> str:
        evidence_id = resources[physical_id].fact_evidence[pointer][0]
        return result.evidence[evidence_id].configuration_path

    assert [str(c) for c in resources["vpc-1"].cidr_blocks] == ["10.0.0.0/16", "2001:db8:1::/56"]
    assert cited("vpc-1", "/cidr_blocks/1") == "Vpcs[0].Ipv6CidrBlockAssociationSet[1]"
    assert [str(c) for c in resources["subnet-1"].cidr_blocks][1] == "2001:db8:1:1::/64"
    assert cited("subnet-1", "/cidr_blocks/1") == "Subnets[0].Ipv6CidrBlockAssociationSet[0]"
    assert [str(a) for a in resources["eni-1"].ipv6_addresses] == ["2001:db8:1:1::10"]
    assert cited("eni-1", "/ipv6_addresses/0") == "NetworkInterfaces[0].Ipv6Addresses[0]"
    egress, prefix = resources["rtb-6"].routes
    assert egress.destination == "::/0" and egress.prefix_list_id is None
    assert prefix.prefix_list_id == "pl-s3"
    snapshot_of(result)  # the subnet's IPv6 CIDR lies inside the VPC's


def test_failed_rule_operation_falls_back_to_permissions_with_limitation():
    items = {**FULL}
    del items[Op.DESCRIBE_SECURITY_GROUP_RULES]
    result = region(items, {Op.DESCRIBE_SECURITY_GROUP_RULES: "denied"})
    group = by_id(result)["sg-1"]
    assert [r.protocol for r in group.ingress_rules] == ["tcp", "tcp"]
    assert group.ingress_rules[1].referenced_group_id == "sg-2"
    assert group.egress_rules[0].direction is RuleDirection.EGRESS
    assert group.attributes["limitations"][0] == FALLBACK_LIMITATION
    assert group.attributes["rules_source"] == "DescribeSecurityGroups"
    cited = result.evidence[group.fact_evidence["/ingress_rules/1"][0]]
    assert cited.configuration_path == "SecurityGroups[0].IpPermissions[1]"
    snapshot_of(result)


def test_parents_fall_back_to_the_vpc_then_the_root_without_being_invented():
    missing_vpc = region({Op.DESCRIBE_SECURITY_GROUPS: [SG], Op.DESCRIBE_SECURITY_GROUP_RULES: []})
    (root,) = missing_vpc.architecture.resources
    assert root.attributes["unresolved_parent"]["id"] == "vpc-1"
    assert not any(r.identity.physical_id == "vpc-1" for r in flatten([root]))

    under_vpc = region({Op.DESCRIBE_VPCS: [VPC], Op.DESCRIBE_NETWORK_INTERFACES: [ENI]})
    (vpc,) = under_vpc.architecture.resources
    (eni,) = vpc.children
    assert eni.attributes["unresolved_parent"]["kind"] == "subnet"

    detached = region({Op.DESCRIBE_INTERNET_GATEWAYS: [{"InternetGatewayId": "igw-x"}]})
    (gateway,) = detached.architecture.resources
    assert "unresolved_parent" not in gateway.attributes


def test_malformed_values_degrade_with_limitations_instead_of_failing():
    group = {
        "GroupId": "sg-bad",
        "VpcId": "vpc-1",
        "IpPermissions": [
            {
                "IpProtocol": "tcp",
                "FromPort": 99999,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "nope"}],
            }
        ],
    }
    eni = {**ENI, "Ipv6Addresses": [{"Ipv6Address": "10.0.1.6"}]}
    nat = {**NAT_GATEWAY, "NatGatewayAddresses": []}  # available and public, with no address
    result = region(
        {
            Op.DESCRIBE_VPCS: [VPC],
            Op.DESCRIBE_SECURITY_GROUPS: [group],
            Op.DESCRIBE_SECURITY_GROUP_RULES: [],
            Op.DESCRIBE_NETWORK_INTERFACES: [eni],
            Op.DESCRIBE_NAT_GATEWAYS: [nat],
        },
        {Op.DESCRIBE_SECURITY_GROUP_RULES: "x"},
    )
    gateway = by_id(result)["nat-1"]
    assert gateway.connectivity is None and "/connectivity" not in gateway.fact_evidence
    assert "ConnectivityType was omitted" in gateway.attributes["limitations"][0]
    interface = by_id(result)["eni-1"]
    assert interface.ipv6_addresses == []
    assert "Ipv6Addresses[0]: invalid value" in interface.attributes["limitations"][0]
    bad = by_id(result)["sg-bad"]
    (rule,) = bad.ingress_rules
    assert rule.cidr is None and rule.from_port is None and rule.to_port == 443
    assert any("invalid value for cidr, from_port" in m for m in bad.attributes["limitations"])
    snapshot_of(result)
