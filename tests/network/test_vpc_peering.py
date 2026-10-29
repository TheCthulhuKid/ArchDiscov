from functools import partial

import pytest

from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision
from archdiscov.services.network_routes import evaluate_vpc_route
from tests.support.fixture_edits import edited_index
from tests.support.network_snapshot import resource_id

D = NetworkDecision
B = NetworkBlockerCode
DATA = {"vpc_id": "vpc-data", "owner_id": "123456789012", "cidr_blocks": ["10.3.0.0/16"]}
peering_index = partial(edited_index, "vpc-peering.json")


def app_to_data(index, address="10.3.1.5"):
    return evaluate_vpc_route(index, resource_id("subnet-app"), address, resource_id("vpc-data"))


def test_an_active_peering_route_leads_into_the_peer_vpc_in_both_directions():
    index = peering_index()
    forward = app_to_data(index)
    assert forward.decision == D.ALLOW
    assert forward.evidence_ids == [
        "pcx-accepter",
        "pcx-requester",
        "pcx-status",
        "rtb-app-association",
        "rtb-app-route-1",
    ]
    back = evaluate_vpc_route(index, resource_id("subnet-data"), "10.2.1.5", resource_id("vpc-app"))
    assert back.decision == D.ALLOW
    assert "rtb-data-route-1" in back.evidence_ids


@pytest.mark.parametrize(
    ("changes", "expected", "named"),
    [
        ({"state": "pending-acceptance"}, (D.DENY, B.DENIED), "pending-acceptance"),
        (
            {"accepter": {**DATA, "owner_id": "210987654321"}},
            (D.UNKNOWN, B.MISSING_EVIDENCE),
            "210987654321",
        ),
        ({"accepter": {**DATA, "cidr_blocks": ["10.3.0.0/24"]}}, (D.DENY, B.DENIED), "CIDRs"),
    ],
    ids=["pending-acceptance", "other-account", "outside-peer-cidrs"],
)
def test_the_connection_decides_as_recorded(changes, expected, named):
    result = app_to_data(peering_index({"pcx-app-data": changes}))
    [blocker] = result.blockers
    assert (result.decision, blocker.code) == expected
    assert named in blocker.message
    if blocker.code == B.DENIED:
        assert {"rtb-app-route-1"} <= set(blocker.evidence_ids)
