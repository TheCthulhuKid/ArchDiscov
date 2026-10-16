from archdiscov.ingestion.network.resources import FALLBACK_LIMITATION
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.enums import ResourceType, RouteTargetKind, RuleDirection
from tests.support.network_data import ENI, FULL, SG, VPC, by_id, flatten, region, snapshot_of


def test_vpc_scoped_resources_are_children_of_the_vpc_and_enis_of_subnets():
    result = region(FULL)
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
    snapshot_of(result)  # integrity: citations and identities resolve


def test_route_citations_point_at_the_specific_route():
    result = region(FULL)
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


def test_rules_prefer_rule_api_and_cite_the_rule_item():
    result = region(FULL)
    group = by_id(result)["sg-1"]
    assert [r.rule_id for r in group.ingress_rules] == ["sgr-1"]
    assert group.egress_rules == []  # sgr-9 belongs to another group
    assert group.attributes["rules_source"] == "DescribeSecurityGroupRules"
    assert "limitations" not in group.attributes
    cited = result.evidence[group.fact_evidence["/ingress_rules/0"][0]]
    assert cited.locator == "ec2:DescribeSecurityGroupRules"
    assert cited.configuration_path == "SecurityGroupRules[0]"


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


def test_descriptions_are_redacted_from_all_evidence():
    result = region(FULL)
    text = snapshot_of(result).model_dump_json()
    assert "hunter2" not in text
    assert "super secret note" not in text
    group_evidence = result.evidence[by_id(result)["sg-1"].evidence_ids[0]]
    assert group_evidence.redacted_paths == [
        "/Description",
        "/IpPermissions/0/IpRanges/0/Description",
    ]


def test_secret_named_tag_values_are_redacted():
    group = {**SG, "Tags": [{"Key": "password", "Value": "tagsecret"}]}
    result = region({Op.DESCRIBE_SECURITY_GROUPS: [group]}, {Op.DESCRIBE_SECURITY_GROUP_RULES: "x"})
    assert "tagsecret" not in snapshot_of(result).model_dump_json()


def test_missing_vpc_places_group_at_root_with_unresolved_parent():
    result = region({Op.DESCRIBE_SECURITY_GROUPS: [SG], Op.DESCRIBE_SECURITY_GROUP_RULES: []})
    (group,) = result.architecture.resources
    assert group.attributes["unresolved_parent"]["id"] == "vpc-1"
    assert not any(r.identity.physical_id == "vpc-1" for r in flatten([group]))


def test_eni_falls_back_to_vpc_then_root():
    under_vpc = region({Op.DESCRIBE_VPCS: [VPC], Op.DESCRIBE_NETWORK_INTERFACES: [ENI]})
    (vpc,) = under_vpc.architecture.resources
    (eni,) = vpc.children
    assert eni.attributes["unresolved_parent"]["kind"] == "subnet"

    at_root = region({Op.DESCRIBE_NETWORK_INTERFACES: [ENI]})
    (root,) = at_root.architecture.resources
    assert root.attributes["unresolved_parent"]["kind"] == "subnet"
    assert not root.children


def test_detached_gateway_stays_at_root_without_invented_parent():
    result = region({Op.DESCRIBE_INTERNET_GATEWAYS: [{"InternetGatewayId": "igw-x"}]})
    (gateway,) = result.architecture.resources
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
    result = region(
        {
            Op.DESCRIBE_VPCS: [VPC],
            Op.DESCRIBE_SECURITY_GROUPS: [group],
            Op.DESCRIBE_SECURITY_GROUP_RULES: [],
        },
        {Op.DESCRIBE_SECURITY_GROUP_RULES: "x"},
    )
    bad = by_id(result)["sg-bad"]
    (rule,) = bad.ingress_rules
    assert rule.cidr is None and rule.from_port is None and rule.to_port == 443
    assert any("invalid value for cidr, from_port" in m for m in bad.attributes["limitations"])
    snapshot_of(result)


def test_zero_results_produce_an_empty_region():
    result = region({op: [] for op in Op})
    assert result.architecture.resources == []
    assert result.evidence == {}
