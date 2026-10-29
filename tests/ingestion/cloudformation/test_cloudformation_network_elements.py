"""Phase 6 network elements imported from CloudFormation templates."""

from archdiscov.ingestion.cloudformation import CloudFormationRequest
from archdiscov.models.architecture import UnknownResource
from archdiscov.models.resources.network import (
    EgressOnlyInternetGateway,
    ManagedPrefixList,
    NatGateway,
    TransitGatewayAttachment,
    TransitGatewayRouteTable,
    VpcEndpoint,
    VpcPeeringConnection,
)
from archdiscov.services import collect_snapshots
from tests.paths import FIXTURES_ROOT
from tests.support.network_data import flatten

FIXTURES = FIXTURES_ROOT / "cloudformation"


def imported(path):
    request = CloudFormationRequest(path=path, account_id="123456789012", region="eu-west-1")
    snapshot = collect_snapshots(request)[0]
    resources = {
        r.identity.logical_address: r
        for tree in snapshot.regions.values()
        for r in flatten(tree.resources)
    }
    return snapshot, resources


def targets(snapshot, source_id):
    return {
        d.target.resource_id
        for deps in snapshot.dependencies_by_region.values()
        for d in deps
        if d.source_id == source_id
    }


def reasons(snapshot, resource_id):
    return {
        value.reason
        for operation in snapshot.coverage.operations
        for value in operation.unresolved_values
        if value.resource_id == resource_id
    }


def test_template_maps_nat_and_egress_only_gateways_with_their_references(tmp_path):
    snapshot, resources = imported(FIXTURES / "egress-gateways.yaml")
    nat, egress_only = resources["Nat"], resources["EgressOnly"]
    assert isinstance(nat, NatGateway) and isinstance(egress_only, EgressOnlyInternetGateway)
    assert nat in resources["Public"].children and egress_only in resources["Egress"].children
    assert nat.connectivity == "public" and nat.name == "public-nat"
    cited = snapshot.evidence[nat.fact_evidence["/connectivity"][0]]
    assert cited.configuration_path == "/Resources/Nat/Properties/ConnectivityType"
    assert isinstance(resources["NatAddress"], UnknownResource)  # the EIP stays unknown
    assert targets(snapshot, nat.id) == {resources["Public"].id, resources["NatAddress"].id}

    # A value the model rejects keeps the declaration, as an unknown resource with a reason.
    template = (FIXTURES / "egress-gateways.yaml").read_text()
    path = tmp_path / "template.yaml"
    path.write_text(template.replace("ConnectivityType: public", "ConnectivityType: Public"))
    snapshot, resources = imported(path)
    rejected = resources["Nat"]
    assert isinstance(rejected, UnknownResource) and rejected.name == "public-nat"
    assert "Typed resource validation failed; only its type is retained." in reasons(
        snapshot, rejected.id
    )


def test_template_peering_refers_to_both_vpcs_without_inventing_their_ids():
    snapshot, resources = imported(FIXTURES / "vpc-peering.yaml")
    connection = resources["AppToData"]
    assert isinstance(connection, VpcPeeringConnection)
    assert connection in snapshot.regions["eu-west-1"].resources  # a root, in neither VPC
    # `!Ref` names a VPC of the template, not a deployed ID, so the sides stay unset.
    assert connection.requester.vpc_id is None and connection.accepter.vpc_id is None
    assert connection.attributes["peer_region"] == "eu-west-1"
    assert targets(snapshot, connection.id) == {resources["App"].id, resources["Data"].id}


def test_template_maps_endpoints_and_prefix_lists():
    snapshot, resources = imported(FIXTURES / "endpoints-and-prefix-lists.yaml")
    gateway, interface = resources["S3Endpoint"], resources["SqsEndpoint"]
    assert isinstance(gateway, VpcEndpoint) and gateway.endpoint_type == "gateway"
    assert interface.endpoint_type == "interface" and interface in resources["Service"].children
    # `!Ref` items name template resources, not deployed IDs: they are references instead.
    assert gateway.route_table_ids == [] and interface.subnet_ids == []
    assert {resources["ServiceSubnet"].id, resources["EndpointGroup"].id} <= targets(
        snapshot, interface.id
    )

    office = resources["Office"]
    assert isinstance(office, ManagedPrefixList) and office.name == "office"
    assert [str(entry.cidr) for entry in office.entries] == ["198.51.100.0/24", "203.0.113.0/24"]
    cited = snapshot.evidence[office.fact_evidence["/entries/1/cidr"][0]]
    assert cited.configuration_path == "/Resources/Office/Properties/Entries/1/Cidr"


def test_template_maps_transit_gateways_and_qualifies_their_route_table():
    snapshot, resources = imported(FIXTURES / "transit-gateway.yaml")
    attachment, table = resources["AppAttachment"], resources["HubRoutes"]
    assert isinstance(attachment, TransitGatewayAttachment)
    assert attachment.attached_resource_type == "vpc"
    # The template's VpcAttachment imports as the one attachment model; its declared type is cited.
    declared = snapshot.evidence[attachment.fact_evidence["/resource_type"][0]]
    assert declared.value == "AWS::EC2::TransitGatewayVpcAttachment"
    # Its one subnet is a `!Ref`, a reference rather than an ID, so the subnets stay unrecorded.
    assert attachment.subnet_ids is None
    assert isinstance(table, TransitGatewayRouteTable)
    children = {resources[name].id for name in ("AppAssociation", "AppPropagation", "Blackhole")}
    to_table = {
        d.source_id
        for deps in snapshot.dependencies_by_region.values()
        for d in deps
        if d.target.resource_id == table.id
    }
    assert children <= to_table
    assert sum("may be incomplete" in reason for reason in reasons(snapshot, table.id)) == 3
