"""IPv6 beside IPv4 in VPCs, subnets, interfaces, routes, and rules."""

from ipaddress import IPv6Network

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import Subnet, Vpc
from archdiscov.models.enums import RouteTargetKind
from archdiscov.models.resource_tree import walk_resources
from archdiscov.models.resources.network import NetworkInterface, SecurityGroupRule
from archdiscov.services import load_snapshot, save_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "snapshots" / "dual-stack.json"
IDENTITY = {"account_id": "123456789012", "region": "eu-west-1", "service": "ec2"}


def test_fixture_loads_typed_round_trips_and_cites_exact_entries(tmp_path):
    snapshot = load_snapshot(FIXTURE)
    resources = {
        resource.identity.physical_id: resource
        for resource in walk_resources(snapshot.regions["eu-west-1"].resources)
    }
    vpc, subnet = resources["vpc-dual"], resources["subnet-dual-public"]
    assert [block.version for block in vpc.cidr_blocks] == [4, 6]
    assert isinstance(subnet.cidr_blocks[1], IPv6Network)
    assert [str(address) for address in resources["eni-dual"].ipv6_addresses] == [
        "2001:db8:1234:1a01::10"
    ]
    defaults = [resources[table].routes[2] for table in ("rtb-dual-public", "rtb-dual-private")]
    assert [(route.destination, route.target_kind) for route in defaults] == [
        ("::/0", RouteTargetKind.INTERNET_GATEWAY),
        ("::/0", RouteTargetKind.EGRESS_ONLY_INTERNET_GATEWAY),
    ]
    assert resources["sg-dual"].ingress_rules[0].cidr.version == 6
    assert resources["acl-dual"].entries[0].cidr.version == 6
    cited = snapshot.evidence[subnet.fact_evidence["/cidr_blocks/1"][0]]
    assert cited.configuration_path == "Subnets[0].Ipv6CidrBlockAssociationSet[0]"

    saved = save_snapshot(snapshot, tmp_path / "dual-stack.json")
    assert load_snapshot(saved) == snapshot


def outside_subnet():
    subnet = Subnet(
        identity={**IDENTITY, "physical_id": "subnet-1"}, cidr_blocks=["2001:db8:ffff::/64"]
    )
    Vpc(
        identity={**IDENTITY, "physical_id": "vpc-1"},
        cidr_blocks=["2001:db8:1234:1a00::/56"],
        children=[subnet],
    )


@pytest.mark.parametrize(
    "build",
    [
        outside_subnet,
        lambda: NetworkInterface(
            identity={**IDENTITY, "physical_id": "eni-1"}, ipv6_addresses=["10.0.0.5"]
        ),
        lambda: SecurityGroupRule(direction="ingress", cidr="::/0", prefix_list_id="pl-1"),
    ],
    ids=["subnet-outside-vpc", "ipv4-as-ipv6", "cidr-and-prefix-list"],
)
def test_impossible_states_are_rejected(build):
    with pytest.raises(ValidationError):
        build()
