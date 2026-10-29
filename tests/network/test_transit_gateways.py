from functools import partial

import pytest

from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision
from archdiscov.services.network_routes import evaluate_vpc_route
from tests.support.fixture_edits import S3_ENTRIES, edited_index, prefix_list_index
from tests.support.network_snapshot import resource_id

D = NetworkDecision
B = NetworkBlockerCode
transit_index = partial(edited_index, "transit-gateway.json")
# rtb-app sends every address to the gateway, so its blackhole route is selected.
ALL_TO_GATEWAY = {
    "rtb-app": {
        "routes": [
            {"destination": "10.2.0.0/16", "target_kind": "local", "state": "active"},
            {
                "destination": "0.0.0.0/0",
                "target_kind": "transit_gateway",
                "target_id": "tgw-main",
                "state": "active",
            },
        ]
    }
}
# An import that left a value of the attachment out, such as a `!Ref` subnet.
IMPORT_LIMITATION = {
    "account_id": "123456789012",
    "region": "eu-west-1",
    "service": "ec2",
    "operation": "ImportCloudFormationTemplate",
    "status": "incomplete",
    "result_count": 1,
    "unresolved_values": [
        {
            "resource_id": resource_id("tgw-attach-app"),
            "configuration_path": "/Resources/AppAttachment/Properties/SubnetIds/1",
            "reason": "The value is a reference.",
        }
    ],
    "started_at": "2026-10-26T08:00:00Z",
    "completed_at": "2026-10-26T08:01:00Z",
}


def app_to_data(index, address="10.3.1.5"):
    return evaluate_vpc_route(index, resource_id("subnet-app"), address, resource_id("vpc-data"))


def test_a_transit_gateway_route_leads_into_the_attached_vpc_in_both_directions():
    index = transit_index()
    forward = app_to_data(index)
    assert forward.decision == D.ALLOW
    assert forward.evidence_ids == [
        "rtb-app-association",
        "rtb-app-route-1",
        "subnet-app",
        "subnet-app-tgw",
        "tgw",
        "tgw-attach-app",
        "tgw-attach-app-subnet",
        "tgw-attach-data",
        "tgw-rtb-association",
        "tgw-rtb-route-1",
        "tgw-rtb-search",
    ]
    back = evaluate_vpc_route(index, resource_id("subnet-data"), "10.2.1.5", resource_id("vpc-app"))
    assert back.decision == D.ALLOW
    assert {"rtb-data-route-1", "tgw-rtb-route-0"} <= set(back.evidence_ids)
    # Into a third VPC, which could forward it on, is a second hop: a deeper chain.
    third = evaluate_vpc_route(index, resource_id("subnet-app"), "10.3.1.5", resource_id("vpc-x"))
    assert (third.decision, third.blockers[0].code) == (D.UNKNOWN, B.UNSUPPORTED_ELEMENT)
    assert "another hop" in third.blockers[0].message


@pytest.mark.parametrize(
    ("variant", "address", "expected", "named"),
    [
        ({"changes": ALL_TO_GATEWAY}, "192.168.1.1", (D.DENY, B.DENIED), "blackhole"),
        (
            {"changes": {"subnet-app": {"availability_zone": "eu-west-1b"}}},
            "10.3.1.5",
            (D.DENY, B.DENIED),
            "eu-west-1b",
        ),
        (
            {"without": {"tgw-rtb-association"}},
            "10.3.1.5",
            (D.UNKNOWN, B.MISSING_EVIDENCE),
            "associ",
        ),
        (
            {"changes": {"tgw-rtb-main": {"routes_complete": False}}},
            "10.3.1.5",
            (D.UNKNOWN, B.MISSING_EVIDENCE),
            "not all recorded",
        ),
        (
            {"coverage": [IMPORT_LIMITATION]},
            "10.3.1.5",
            (D.UNKNOWN, B.MISSING_EVIDENCE),
            "subnets",
        ),
    ],
    ids=["blackhole", "no-subnet-in-zone", "no-association", "incomplete-search", "import-limit"],
)
def test_the_hop_decides_as_recorded(variant, address, expected, named):
    result = app_to_data(transit_index(**variant), address)
    [blocker] = result.blockers
    assert (result.decision, blocker.code) == expected
    assert named in blocker.message
    if blocker.code == B.DENIED:
        assert {"rtb-app-route-1", "tgw"} <= set(blocker.evidence_ids)


def test_a_transit_gateway_outside_the_snapshot_is_unknown_naming_it():
    result = evaluate_vpc_route(
        prefix_list_index(S3_ENTRIES),
        resource_id("subnet-svc"),
        "198.51.100.7",
        resource_id("vpc-other"),
    )
    [blocker] = result.blockers
    assert (result.decision, blocker.code) == (D.UNKNOWN, B.MISSING_EVIDENCE)
    assert "tgw-main" in blocker.message
