from archdiscov.models.enums import RouteTargetKind
from archdiscov.models.resources.network import (
    InternetGateway,
    NetworkAcl,
    NetworkInterface,
    RouteTable,
    SecurityGroup,
)
from archdiscov.services import load_snapshot, save_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


def test_network_fixture_round_trips_with_typed_children(tmp_path):
    snapshot = load_snapshot(FIXTURES / "network.json")
    by_physical = {
        r.identity.physical_id: r for r in flatten(snapshot.regions["eu-west-1"].resources)
    }
    assert isinstance(by_physical["rtb-main"], RouteTable)
    assert isinstance(by_physical["igw-main"], InternetGateway)
    assert isinstance(by_physical["acl-main"], NetworkAcl)
    assert isinstance(by_physical["sg-web"], SecurityGroup)
    assert isinstance(by_physical["eni-web"], NetworkInterface)
    assert by_physical["rtb-main"].routes[1].target_kind is RouteTargetKind.INTERNET_GATEWAY
    assert by_physical["sg-web"].ingress_rules[1].referenced_group_id == "sg-db"
    assert by_physical["eni-web"].private_ip_addresses[0].address == "10.0.1.5"

    saved = save_snapshot(snapshot, tmp_path / "out.json")
    assert load_snapshot(saved) == snapshot
