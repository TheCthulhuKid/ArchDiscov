"""VPC endpoints, managed prefix lists, and the routes and rules naming them."""

import pytest
from pydantic import ValidationError

from archdiscov.models.enums import VpcEndpointType
from archdiscov.models.resource_tree import walk_resources
from archdiscov.models.resources.network import ManagedPrefixList, VpcEndpoint
from archdiscov.services import load_snapshot, save_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "snapshots" / "endpoints-and-prefix-lists.json"
IDENTITY = {"account_id": "123456789012", "region": "eu-west-1", "service": "ec2"}


def test_fixture_loads_typed_round_trips_and_cites_exact_entries(tmp_path):
    snapshot = load_snapshot(FIXTURE)
    resources = {
        resource.identity.physical_id: resource
        for resource in walk_resources(snapshot.regions["eu-west-1"].resources)
    }
    gateway, interface = resources["vpce-s3"], resources["vpce-sqs"]
    assert isinstance(gateway, VpcEndpoint)
    assert gateway.endpoint_type is VpcEndpointType.GATEWAY
    assert gateway.route_table_ids == ["rtb-svc"]
    assert interface.network_interface_ids == ["eni-vpce"]
    assert interface.security_group_ids == ["sg-endpoint"]

    aws_list, office = resources["pl-s3"], resources["pl-office"]
    assert isinstance(aws_list, ManagedPrefixList)
    # An AWS-managed list is valid with its entries unrecorded, never as an empty list.
    assert aws_list.owner_id == "AWS"
    assert aws_list.entries is None
    assert [str(entry.cidr) for entry in office.entries] == ["198.51.100.0/24", "203.0.113.0/24"]

    table, group = resources["rtb-svc"], resources["sg-endpoint"]
    assert [route.prefix_list_id for route in table.routes] == [None, "pl-s3", "pl-office"]
    assert table.routes[1].destination == "pl-s3"  # the reported form is kept
    assert group.ingress_rules[0].prefix_list_id == "pl-office"
    cited = snapshot.evidence[table.fact_evidence["/routes/2"][0]]
    assert cited.configuration_path == "RouteTables[0].Routes[2]"

    saved = save_snapshot(snapshot, tmp_path / "endpoints.json")
    assert load_snapshot(saved) == snapshot


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (ManagedPrefixList, {"address_family": "ipv6", "entries": [{"cidr": "10.0.0.0/8"}]}),
        (VpcEndpoint, {"endpoint_type": "gateway", "subnet_ids": ["subnet-1"]}),
        (VpcEndpoint, {"endpoint_type": "interface", "route_table_ids": ["rtb-1"]}),
    ],
    ids=["entry-of-another-family", "gateway-with-subnets", "interface-with-route-tables"],
)
def test_impossible_states_are_rejected(model, fields):
    with pytest.raises(ValidationError):
        model(identity={**IDENTITY, "physical_id": "x-1"}, **fields)
