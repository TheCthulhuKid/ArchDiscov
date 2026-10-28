import pytest

from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision, RouteReach
from archdiscov.services.network_endpoints import EndpointInterfaces, endpoint_interfaces
from archdiscov.services.network_routes import evaluate_subnet_route
from tests.support.fixture_edits import edited_index
from tests.support.network_snapshot import resource_id

D = NetworkDecision
B = NetworkBlockerCode
S3_ADDRESS = "52.218.1.1"
# pl-s3's entry recorded and cited, as GetManagedPrefixListEntries returns it.
S3_ENTRIES = {
    "pl-s3": {
        "entries": [{"cidr": "52.218.0.0/17"}],
        "fact_evidence": {
            "/address_family": ["pl-s3"],
            "/owner_id": ["pl-s3"],
            "/entries/0": ["pl-s3-entry-0"],
        },
    }
}


def endpoint_index(changes=None, **edits):
    return edited_index(
        "endpoints-and-prefix-lists.json", {**S3_ENTRIES, **(changes or {})}, **edits
    )


def service_route(index, address=S3_ADDRESS, reach=RouteReach.VPC_ENDPOINT):
    return evaluate_subnet_route(index, resource_id("subnet-svc"), address, reach=reach)


def test_an_s3_address_reaches_the_gateway_endpoint_through_its_prefix_list():
    index = endpoint_index()
    result = service_route(index)
    assert result.decision == D.ALLOW
    # The route and its entry, the association, the endpoint, its table edge, and the list.
    assert result.evidence_ids == [
        "pl-s3",
        "pl-s3-entry-0",
        "rtb-association",
        "rtb-s3",
        "vpce-s3",
        "vpce-s3-route-table",
    ]
    # An address outside the list's entries takes another route, and a local question on
    # the endpoint route is answered by where it leads.
    assert service_route(index, "198.51.100.7").decision == D.DENY
    assert service_route(index, reach=RouteReach.LOCAL).decision == D.DENY


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ({"changes": {"vpce-s3": {"state": "Deleted"}}}, (D.DENY, B.DENIED)),
        ({"without": {"vpce-s3-route-table"}}, (D.UNKNOWN, B.MISSING_EVIDENCE)),
        (
            {"changes": {"vpce-s3": {"endpoint_type": "gateway_load_balancer"}}},
            (D.UNKNOWN, B.UNSUPPORTED_ELEMENT),
        ),
    ],
    ids=["deleted", "not-associated", "gateway-load-balancer"],
)
def test_the_endpoint_decides_as_recorded(variant, expected):
    result = service_route(endpoint_index(**variant))
    [blocker] = result.blockers
    assert (result.decision, blocker.code) == expected
    if blocker.code == B.DENIED:
        assert {"rtb-s3", "vpce-s3"} <= set(blocker.evidence_ids)
    # A Gateway Load Balancer endpoint stays unsupported for any question.
    if blocker.code == B.UNSUPPORTED_ELEMENT:
        local = service_route(endpoint_index(**variant), reach=RouteReach.LOCAL)
        assert local.blockers[0].code == B.UNSUPPORTED_ELEMENT


def test_an_interface_endpoint_resolves_to_its_interfaces_and_groups_with_citations():
    index = endpoint_index()
    assert endpoint_interfaces(index, resource_id("vpce-sqs")) == EndpointInterfaces(
        network_interface_ids=(resource_id("eni-vpce"),),
        security_group_ids=(resource_id("sg-endpoint"),),
        evidence_ids=("vpce-sqs", "vpce-sqs-group", "vpce-sqs-interface"),
    )
    assert endpoint_interfaces(index, resource_id("vpce-s3")).decision == D.UNKNOWN
