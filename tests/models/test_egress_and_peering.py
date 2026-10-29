"""NAT gateways, egress-only internet gateways, and peering connections as recorded facts."""

import pytest
from pydantic import ValidationError

from archdiscov.models.enums import NatConnectivity
from archdiscov.models.resource_tree import walk_resources
from archdiscov.models.resources.network import (
    EgressOnlyInternetGateway,
    NatGateway,
    VpcPeeringConnection,
)
from archdiscov.services import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
IDENTITY = {"account_id": "123456789012", "region": "eu-west-1", "service": "ec2"}
SIDES = {
    "requester": {"vpc_id": "vpc-app", "owner_id": "123456789012"},
    "accepter": {"vpc_id": "vpc-data", "owner_id": "123456789012"},
}


def by_physical_id(snapshot):
    resources = snapshot.regions["eu-west-1"].resources
    return {resource.identity.physical_id: resource for resource in walk_resources(resources)}


def test_fixtures_load_typed_and_cite_exact_entries():
    egress = load_snapshot(FIXTURES / "egress-gateways.json")
    nat = by_physical_id(egress)["nat-public"]
    assert isinstance(nat, NatGateway)
    assert nat.connectivity is NatConnectivity.PUBLIC
    assert [str(address) for address in nat.public_ips] == ["198.51.100.20"]
    cited = egress.evidence[nat.fact_evidence["/public_ips/0"][0]]
    assert cited.configuration_path == "NatGateways[0].NatGatewayAddresses[0]"
    assert isinstance(by_physical_id(egress)["eigw-main"], EgressOnlyInternetGateway)

    peering = load_snapshot(FIXTURES / "vpc-peering.json")
    connection = by_physical_id(peering)["pcx-app-data"]
    assert isinstance(connection, VpcPeeringConnection)
    # Same-account is a comparison of the two recorded owners, never an assumption.
    assert connection.requester.owner_id == connection.accepter.owner_id
    cited = peering.evidence[connection.fact_evidence["/accepter"][0]]
    assert cited.configuration_path == "VpcPeeringConnections[0].AccepterVpcInfo"

    # A public NAT gateway still being created has no address yet.
    NatGateway(
        identity={**IDENTITY, "physical_id": "nat-new"}, state="pending", connectivity="public"
    )


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (VpcPeeringConnection, {**SIDES, "accepter": {"vpc_id": "vpc-app"}}),
        (NatGateway, {"state": "available", "connectivity": "public"}),
    ],
    ids=["same-vpc", "public-without-address"],
)
def test_impossible_states_are_rejected(model, fields):
    with pytest.raises(ValidationError):
        model(identity={**IDENTITY, "physical_id": "x-1"}, **fields)
