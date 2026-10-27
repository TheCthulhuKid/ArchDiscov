from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import ExternalTarget, LocalTarget, UnresolvedTarget
from archdiscov.models.enums import AttachmentKind as Kind
from archdiscov.models.enums import DependencyRelationship
from tests.support.network_data import (
    ACCOUNT,
    ATTACHED_ENI,
    EGRESS_ONLY_GATEWAY,
    FULL,
    INSTANCE,
    NAT_GATEWAY,
    PRIVATE_ROUTE_TABLE,
    SG,
    SUBNET,
    VPC,
    by_id,
    region,
    snapshot_of,
)

ITEMS = {
    **FULL,
    Op.DESCRIBE_INSTANCES: [{"Instances": [INSTANCE]}],
    Op.DESCRIBE_NETWORK_INTERFACES: [ATTACHED_ENI],
}


def rid(physical_id: str, service: str = "ec2") -> str:
    return f"{ACCOUNT}/eu-west-1/{service}/physical/{physical_id}"


def deps(result, kind: Kind, source: str | None = None):
    return [
        d
        for d in result.dependencies
        if d.attributes["kind"] == kind.value and (source is None or d.source_id == rid(source))
    ]


def build(items=ITEMS, unavailable=None):
    result = region(items, unavailable)
    snapshot = snapshot_of(result)  # raises if endpoints or citations are invalid
    return result, snapshot


def test_snapshot_with_all_relationships_passes_integrity_validation():
    result, snapshot = build()
    assert snapshot.dependencies_by_region  # validated with the snapshot's integrity checks
    assert all(d.relationship is DependencyRelationship.ATTACHED_TO for d in result.dependencies)
    ids = [d.evidence_ids[0] for d in result.dependencies]
    for dep in result.dependencies:
        assert result.evidence[dep.evidence_ids[0]].resource_id == dep.source_id
    assert len(ids) == len(result.dependencies)


def test_associations_gateways_acls_and_interfaces_resolve_locally():
    result, _ = build()
    main, subnet_assoc = deps(result, Kind.ROUTE_TABLE_ASSOCIATION, "rtb-1")
    assert main.target == LocalTarget(resource_id=rid("vpc-1")) and main.attributes["main"] is True
    assert subnet_assoc.target == LocalTarget(resource_id=rid("subnet-1"))
    cited = result.evidence[subnet_assoc.evidence_ids[0]]
    assert cited.configuration_path == "RouteTables[0].Associations[1]"

    (igw,) = deps(result, Kind.INTERNET_GATEWAY_ATTACHMENT)
    assert igw.source_id == rid("igw-1") and igw.target == LocalTarget(resource_id=rid("vpc-1"))

    (eni,) = deps(result, Kind.INSTANCE_INTERFACE)
    assert eni.source_id == rid("eni-1") and eni.target == LocalTarget(resource_id=rid("i-1"))
    assert eni.attributes["status"] == "attached"


def test_network_acl_association_targets_subnet():
    items = {
        **ITEMS,
        Op.DESCRIBE_NETWORK_ACLS: [
            {
                "NetworkAclId": "acl-1",
                "VpcId": "vpc-1",
                "Associations": [{"NetworkAclAssociationId": "aclassoc-1", "SubnetId": "subnet-1"}],
            }
        ],
    }
    result, _ = build(items)
    (dep,) = deps(result, Kind.NETWORK_ACL_ASSOCIATION)
    assert dep.target == LocalTarget(resource_id=rid("subnet-1"))
    assert dep.attributes["association_id"] == "aclassoc-1"


def test_route_targets_local_igw_and_external_peering():
    result, _ = build()
    routes = {d.attributes["target_kind"]: d for d in deps(result, Kind.ROUTE_TARGET)}
    assert set(routes) == {"internet_gateway", "vpc_peering"}  # "local" routes add no link
    assert routes["internet_gateway"].target == LocalTarget(resource_id=rid("igw-1"))
    assert routes["internet_gateway"].attributes["destination"] == "0.0.0.0/0"
    peering = routes["vpc_peering"].target
    assert isinstance(peering, ExternalTarget)
    assert peering.identifier == "pcx-1"
    assert peering.aws_resource_type == "AWS::EC2::VPCPeeringConnection"
    cited = result.evidence[routes["vpc_peering"].evidence_ids[0]]
    assert cited.configuration_path == "RouteTables[0].Routes[2]"


def test_unsupported_and_unparseable_route_targets():
    table = {
        "RouteTableId": "rtb-2",
        "VpcId": "vpc-1",
        "Routes": [
            {"DestinationCidrBlock": "10.1.0.0/16", "NatGatewayId": "nat-1"},
            {"DestinationCidrBlock": "10.2.0.0/16", "TransitGatewayId": "tgw-1"},
            {"DestinationPrefixListId": "pl-s3", "GatewayId": "vpce-1"},
            {"DestinationIpv6CidrBlock": "::/0", "EgressOnlyInternetGatewayId": "eigw-1"},
            {"DestinationCidrBlock": "10.4.0.0/16", "State": "blackhole"},
            {"DestinationCidrBlock": "10.5.0.0/16", "GatewayId": "igw-missing"},
        ],
    }
    result, _ = build({Op.DESCRIBE_VPCS: [VPC], Op.DESCRIBE_ROUTE_TABLES: [table]})
    routes = deps(result, Kind.ROUTE_TARGET)
    targets = [d.target for d in routes]
    # NAT and egress-only gateways are collected, so a missing one is unresolved, not external.
    nat, transit, endpoint, egress_only = targets[:4]
    assert "nat-1 was not present" in nat.reason
    assert "eigw-1 was not present" in egress_only.reason
    assert routes[3].attributes["target_kind"] == "egress_only_internet_gateway"
    assert transit.aws_resource_type == "AWS::EC2::TransitGateway"
    assert endpoint.aws_resource_type == "AWS::EC2::VPCEndpoint"
    (prefix_list,) = deps(result, Kind.PREFIX_LIST_REFERENCE)
    assert prefix_list.target.identifier == "pl-s3"
    assert prefix_list.evidence_ids == routes[2].evidence_ids
    blackhole, missing = targets[4:]
    assert isinstance(blackhole, UnresolvedTarget)
    assert blackhole.source_path == "RouteTables[0].Routes[4]"
    assert "no recognizable target" in blackhole.reason
    assert isinstance(missing, UnresolvedTarget)
    assert "igw-missing was not present" in missing.reason


def test_nat_and_egress_only_gateways_are_placed_linked_and_reached_by_routes():
    items = {
        **ITEMS,
        Op.DESCRIBE_ROUTE_TABLES: [*FULL[Op.DESCRIBE_ROUTE_TABLES], PRIVATE_ROUTE_TABLE],
        Op.DESCRIBE_NAT_GATEWAYS: [NAT_GATEWAY],
        Op.DESCRIBE_EGRESS_ONLY_INTERNET_GATEWAYS: [EGRESS_ONLY_GATEWAY],
    }
    result, _ = build(items)
    resources = by_id(result)
    nat = resources["nat-1"]
    assert nat.connectivity == "public" and nat.network_interface_id == "eni-nat"
    assert [str(ip) for ip in nat.public_ips] == ["198.51.100.20"]
    assert nat in resources["subnet-1"].children
    assert resources["eigw-1"] in resources["vpc-1"].children

    (subnet,) = deps(result, Kind.NAT_GATEWAY_SUBNET)
    assert subnet.target == LocalTarget(resource_id=rid("subnet-1"))
    (interface,) = deps(result, Kind.NAT_GATEWAY_INTERFACE)
    assert "eni-nat was not present" in interface.target.reason
    path = "NatGateways[0].NatGatewayAddresses[0].NetworkInterfaceId"
    assert interface.target.source_path == path
    (attachment,) = deps(result, Kind.EGRESS_ONLY_INTERNET_GATEWAY_ATTACHMENT)
    assert attachment.target == LocalTarget(resource_id=rid("vpc-1"))
    assert attachment.attributes["state"] == "attached"
    to_nat, to_egress_only = deps(result, Kind.ROUTE_TARGET, "rtb-private")
    assert to_nat.target == LocalTarget(resource_id=rid("nat-1"))
    assert to_egress_only.target == LocalTarget(resource_id=rid("eigw-1"))


def test_security_group_membership_local_and_missing():
    result, _ = build()
    memberships = deps(result, Kind.SECURITY_GROUP_MEMBERSHIP, "i-1")
    local, missing = sorted(memberships, key=lambda d: d.evidence_ids[0])
    assert local.target == LocalTarget(resource_id=rid("sg-1"))
    assert isinstance(missing.target, UnresolvedTarget)
    assert missing.target.source_path == "Reservations[0].Instances[0].SecurityGroups[1].GroupId"
    (eni_member,) = deps(result, Kind.SECURITY_GROUP_MEMBERSHIP, "eni-1")
    assert eni_member.target == LocalTarget(resource_id=rid("sg-1"))


def test_security_group_references_local_external_and_prefix_list():
    other = {
        "GroupId": "sg-2",
        "VpcId": "vpc-1",
        "IpPermissions": [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "UserIdGroupPairs": [
                    {"GroupId": "sg-1", "UserId": ACCOUNT},
                    {"GroupId": "sg-foreign", "UserId": "210987654321"},
                ],
                "PrefixListIds": [{"PrefixListId": "pl-1"}],
            }
        ],
    }
    result, _ = build(
        {
            Op.DESCRIBE_VPCS: [VPC],
            Op.DESCRIBE_SECURITY_GROUPS: [SG, other],
            Op.DESCRIBE_SECURITY_GROUP_RULES: [],
        },
        {Op.DESCRIBE_SECURITY_GROUP_RULES: "denied"},
    )
    local, foreign = deps(result, Kind.SECURITY_GROUP_REFERENCE, "sg-2")
    (prefix,) = deps(result, Kind.PREFIX_LIST_REFERENCE, "sg-2")
    assert local.target == LocalTarget(resource_id=rid("sg-1"))
    assert local.attributes["from_port"] == 22 and local.attributes["direction"] == "ingress"
    assert isinstance(foreign.target, ExternalTarget)
    assert foreign.target.account_id == "210987654321"
    assert foreign.target.aws_resource_type == "AWS::EC2::SecurityGroup"
    assert prefix.target.identifier == "pl-1"
    assert prefix.target.aws_resource_type == "AWS::EC2::PrefixList"
    assert prefix.attributes["direction"] == "ingress"
    cited = result.evidence[local.evidence_ids[0]]
    assert cited.configuration_path == "SecurityGroups[1].IpPermissions[0]"
    (own,) = deps(result, Kind.SECURITY_GROUP_REFERENCE, "sg-1")
    assert own.target == LocalTarget(resource_id=rid("sg-2"))


def test_failed_counterpart_operation_yields_unresolved_not_local():
    items = {k: v for k, v in ITEMS.items() if k is not Op.DESCRIBE_ROUTE_TABLES}
    items.pop(Op.DESCRIBE_INTERNET_GATEWAYS)
    result, _ = build(
        items, {Op.DESCRIBE_INTERNET_GATEWAYS: "denied", Op.DESCRIBE_ROUTE_TABLES: "x"}
    )
    assert deps(result, Kind.ROUTE_TARGET) == []  # route tables absent: no guessed facts
    assert "rtb-1" not in by_id(result)
    items = {**ITEMS}
    del items[Op.DESCRIBE_INTERNET_GATEWAYS]
    result, _ = build(items, {Op.DESCRIBE_INTERNET_GATEWAYS: "denied"})
    igw_route = next(
        d
        for d in deps(result, Kind.ROUTE_TARGET)
        if d.attributes["target_kind"] == "internet_gateway"
    )
    assert isinstance(igw_route.target, UnresolvedTarget)
    assert "DescribeInternetGateways did not complete" in igw_route.target.reason
    assert igw_route.target.source_path == "RouteTables[0].Routes[1].GatewayId"


def test_missing_subnet_leaves_unresolved_association_without_inventing_parent():
    items = {Op.DESCRIBE_VPCS: [VPC], Op.DESCRIBE_ROUTE_TABLES: [FULL[Op.DESCRIBE_ROUTE_TABLES][0]]}
    result, _ = build(items)
    assoc = deps(result, Kind.ROUTE_TABLE_ASSOCIATION, "rtb-1")[1]
    assert isinstance(assoc.target, UnresolvedTarget)
    assert "subnet-1 was not present" in assoc.target.reason
    assert SUBNET["SubnetId"] not in by_id(result)
