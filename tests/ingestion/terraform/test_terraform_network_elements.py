"""Phase 6 network elements imported from Terraform state and plans."""

from archdiscov.ingestion.import_fields import incomplete_list
from archdiscov.ingestion.terraform import TerraformStateRequest
from archdiscov.models.architecture import UnknownResource
from archdiscov.models.resources.network import (
    EgressOnlyInternetGateway,
    ManagedPrefixList,
    NatGateway,
    TransitGateway,
    TransitGatewayAttachment,
    TransitGatewayRouteTable,
    VpcEndpoint,
    VpcPeeringConnection,
)
from archdiscov.services import collect_snapshots
from tests.paths import FIXTURES_ROOT
from tests.support.network_data import flatten
from tests.support.terraform_data import CONTEXT, collect

FIXTURES = FIXTURES_ROOT / "terraform"


def state(name):
    request = TerraformStateRequest(
        path=FIXTURES / f"{name}.state.json", account_id="123456789012", region="eu-west-1"
    )
    return collect_snapshots(request, context=CONTEXT)[0]


def by_key(snapshot, key):
    return {key(r): r for tree in snapshot.regions.values() for r in flatten(tree.resources)}


def references(snapshot):
    """(source, target, field) for each configuration reference, by physical ID or address."""
    names = {
        r.id: r.identity.physical_id or r.identity.logical_address
        for r in by_key(snapshot, lambda r: r.id).values()
    }
    return {
        (names[d.source_id], names.get(d.target.resource_id), d.attributes["field"])
        for deps in snapshot.dependencies_by_region.values()
        for d in deps
        if d.target.kind == "local"
    }


def reasons(snapshot, resource_id):
    return {
        value.reason
        for operation in snapshot.coverage.operations
        for value in operation.unresolved_values
        if value.resource_id == resource_id
    }


def test_state_maps_nat_and_egress_only_gateways_with_their_references():
    snapshot = state("egress-gateways")
    resources = by_key(snapshot, lambda r: r.identity.physical_id)
    nat, egress_only = resources["nat-public"], resources["eigw-main"]
    assert isinstance(nat, NatGateway) and isinstance(egress_only, EgressOnlyInternetGateway)
    assert nat in resources["subnet-public"].children
    assert egress_only in resources["vpc-egress"].children
    assert (nat.connectivity, nat.network_interface_id) == ("public", "eni-nat")
    assert [str(ip) for ip in nat.public_ips] == ["198.51.100.20"]
    assert nat.state is None  # Terraform declares no state, so none is assumed
    cited = snapshot.evidence[nat.fact_evidence["/public_ips/0"][0]]
    assert cited.configuration_path.endswith("/values/public_ip")
    assert {
        ("nat-public", "subnet-public", "subnet_id"),
        ("eigw-main", "vpc-egress", "vpc_id"),
    } <= references(snapshot)


def test_plan_keeps_unknown_nat_gateway_values_indeterminate():
    snapshot = collect(FIXTURES / "egress-gateways.plan.json")
    nat = by_key(snapshot, lambda r: r.identity.logical_address)["aws_nat_gateway.public"]
    assert isinstance(nat, NatGateway) and nat.connectivity == "public"
    assert nat.identity.physical_id is None and nat.network_interface_id is None
    assert "/network_interface_id" not in nat.fact_evidence and nat.public_ips == []
    unknown = {
        item.configuration_path.rsplit("/", 1)[-1]
        for item in snapshot.evidence.values()
        if item.value_status == "unresolved" and item.resource_id == nat.id
    }
    assert {"id", "network_interface_id", "public_ip", "subnet_id"} <= unknown
    assert (
        "aws_nat_gateway.public",
        "aws_subnet.public",
        "configuration_expression",
    ) in references(snapshot)


def test_state_maps_peering_sides_without_assuming_the_requester_owner():
    snapshot = state("vpc-peering")
    resources = [r for tree in snapshot.regions.values() for r in flatten(tree.resources)]
    (connection,) = [r for r in resources if isinstance(r, VpcPeeringConnection)]
    assert connection.state == "active" and connection.attributes["peer_region"] == "eu-west-1"
    assert connection.requester.vpc_id == "vpc-app"
    assert connection.requester.owner_id is None  # not declared, so not assumed
    assert (connection.accepter.vpc_id, connection.accepter.owner_id) == (
        "vpc-data",
        "123456789012",
    )
    cited = snapshot.evidence[connection.fact_evidence["/accepter/vpc_id"][0]]
    assert cited.configuration_path.endswith("/values/peer_vpc_id")
    assert {
        ("pcx-app-data", "vpc-app", "requester/vpc_id"),
        ("pcx-app-data", "vpc-data", "accepter/vpc_id"),
    } <= references(snapshot)
    # The accepter declares the same connection from its side; it stays a separate summary.
    (accepter,) = [r for r in resources if isinstance(r, UnknownResource)]
    assert accepter.aws_resource_type == "aws_vpc_peering_connection_accepter"


def test_state_maps_endpoints_and_prefix_lists_and_qualifies_a_separate_entry():
    snapshot = state("endpoints-and-prefix-lists")
    resources = by_key(snapshot, lambda r: r.identity.physical_id)
    gateway, interface = resources["vpce-s3"], resources["vpce-sqs"]
    assert isinstance(gateway, VpcEndpoint) and gateway.endpoint_type == "gateway"
    assert gateway in resources["vpc-svc"].children and gateway.route_table_ids == ["rtb-svc"]
    assert (interface.subnet_ids, interface.security_group_ids) == (["subnet-svc"], ["sg-endpoint"])
    assert {
        ("vpce-s3", "rtb-svc", "route_table_ids"),
        ("vpce-sqs", "subnet-svc", "subnet_ids"),
        ("vpce-sqs", "sg-endpoint", "security_group_ids"),
    } <= references(snapshot)

    office = resources["pl-office"]
    assert isinstance(office, ManagedPrefixList) and office.version == 3
    assert [str(entry.cidr) for entry in office.entries] == ["198.51.100.0/24", "203.0.113.0/24"]
    cited = snapshot.evidence[office.fact_evidence["/entries/0/cidr"][0]]
    assert cited.value == "198.51.100.0/24"  # the CIDR only, never the free-text description

    # A separate entry resource adds to pl-partners, so its own entries are not the whole list.
    partners = resources["pl-partners"]
    assert partners.entries is None and "/entries/0/cidr" not in partners.fact_evidence
    assert ("pl-partners,198.18.0.0/15", "pl-partners", "prefix_list_id") in references(snapshot)
    assert incomplete_list("entries") in reasons(snapshot, partners.id)


def test_state_maps_transit_gateways_and_keeps_declared_children_separate():
    snapshot = state("transit-gateway")
    resources = by_key(snapshot, lambda r: r.identity.physical_id)
    gateway, attachment = resources["tgw-main"], resources["tgw-attach-app"]
    table = resources["tgw-rtb-main"]
    assert isinstance(gateway, TransitGateway)
    assert gateway.association_default_route_table_id == "tgw-rtb-main"
    assert isinstance(attachment, TransitGatewayAttachment)
    assert (attachment.attached_resource_type, attachment.attached_resource_id) == (
        "vpc",
        "vpc-app",
    )
    cited = snapshot.evidence[attachment.fact_evidence["/attached_resource_type"][0]]
    assert cited.value == "aws_ec2_transit_gateway_vpc_attachment"  # the type states it
    assert attachment.subnet_ids == ["subnet-app"] and "/subnet_ids/0" in attachment.fact_evidence
    assert isinstance(table, TransitGatewayRouteTable) and table.routes_complete is None
    links = references(snapshot)
    assert {
        ("tgw-attach-app", "tgw-main", "transit_gateway_id"),
        ("tgw-attach-app", "vpc-app", "attached_resource_id"),
        ("tgw-rtb-main", "tgw-main", "transit_gateway_id"),
        ("tgw-rtb-main_tgw-attach-app", "tgw-rtb-main", "transit_gateway_route_table_id"),
        ("tgw-rtb-main_tgw-attach-app", "tgw-attach-app", "transit_gateway_attachment_id"),
    } <= links
    # A blackhole route names no attachment, so it references only its table.
    assert {target for source, target, _ in links if source == "tgw-rtb-main_192.168.0.0/16"} == {
        "tgw-rtb-main"
    }
    # The children are not folded in: the table's lists are not claimed as complete.
    assert table.associations == table.propagations == table.routes == []
    lists = ("associations", "propagations", "routes")
    assert {incomplete_list(name) for name in lists} <= reasons(snapshot, table.id)
