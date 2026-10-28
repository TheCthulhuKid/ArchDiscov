from functools import partial

import pytest

from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision, RouteReach
from archdiscov.services.network_routes import evaluate_subnet_route
from tests.support.fixture_edits import edited_index
from tests.support.network_snapshot import resource_id

D = NetworkDecision
B = NetworkBlockerCode
INTERNET = "203.0.113.9"
egress_index = partial(edited_index, "egress-gateways.json")
# A private gateway records no public address, so its address citation goes too.
PRIVATE = {
    "connectivity": "private",
    "public_ips": [],
    "fact_evidence": {"/state": ["nat"], "/connectivity": ["nat"], "/subnet_id": ["nat"]},
}


def private_route(index, reach=RouteReach.NAT_GATEWAY):
    return evaluate_subnet_route(index, resource_id("subnet-private"), INTERNET, reach=reach)


def test_a_private_subnet_reaches_the_internet_through_the_nat_gateway_citing_every_hop():
    result = private_route(egress_index())
    assert result.decision == D.ALLOW
    assert result.evidence_ids == [
        "igw-attachment",
        "nat",
        "nat-address",
        "rtb-private-association",
        "rtb-private-route-1",
        "rtb-public-association",
        "rtb-public-route-1",
    ]
    # A NAT gateway never carries inbound traffic: an internet-gateway question denies.
    inbound = private_route(egress_index(), RouteReach.INTERNET_GATEWAY)
    assert (inbound.decision, inbound.blockers[0].element) == (D.DENY, "route:/routes/1")


@pytest.mark.parametrize(
    ("nat", "expected"),
    [
        ({"state": "deleted"}, (D.DENY, B.DENIED)),
        ({"state": "pending"}, (D.UNKNOWN, B.MISSING_EVIDENCE)),
        (PRIVATE, (D.DENY, B.DENIED)),
    ],
    ids=["deleted", "pending", "private"],
)
def test_the_gateways_state_and_connectivity_decide_as_recorded(nat, expected):
    result = private_route(egress_index({"nat-public": nat}))
    [blocker] = result.blockers
    assert (result.decision, blocker.code) == expected
    if blocker.code == B.DENIED:
        assert {"nat", "rtb-private-route-1"} <= set(blocker.evidence_ids)


def test_a_nat_subnet_without_a_recorded_route_table_is_unknown():
    result = private_route(egress_index(without={"rtb-public-association"}))
    assert result.decision == D.UNKNOWN
    assert {blocker.code for blocker in result.blockers} == {B.MISSING_EVIDENCE}
