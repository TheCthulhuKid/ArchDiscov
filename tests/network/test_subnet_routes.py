import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision, RouteReach
from archdiscov.services import load_snapshot
from archdiscov.services.network_routes import evaluate_subnet_route
from tests.paths import FIXTURES_ROOT
from tests.support.network_snapshot import resource_id
from tests.support.route_data import LOCAL, build, gateway, route, table

D = NetworkDecision
B = NetworkBlockerCode
IGW, LOCAL_REACH = RouteReach.INTERNET_GATEWAY, RouteReach.LOCAL
PUBLIC = "203.0.113.9"
INSIDE = "10.0.1.5"


def check(tables, gateways=None, *, address=PUBLIC, reach=IGW, subnet="subnet-1", **kw):
    kw.setdefault("subnets", ("subnet-1", "subnet-2"))
    index = GraphIndex(build(tables, [gateway()] if gateways is None else gateways, **kw))
    return index, evaluate_subnet_route(index, resource_id(subnet), address, reach=reach)


def decision(tables, **kwargs):
    return check(tables, **kwargs)[1].decision


def codes(result):
    return {blocker.code for blocker in result.blockers}


PUBLIC_TABLE = table("rtb-1", [LOCAL, route()], "subnet-1")


# ---- table selection ----------------------------------------------------------------


def test_the_explicit_association_is_used_and_cited_with_the_route_and_gateway():
    index, result = check([PUBLIC_TABLE])
    rtb = index.get_resource(resource_id("rtb-1"))
    [association] = index.incoming_edges(resource_id("subnet-1"))
    [attachment] = index.outgoing_edges(resource_id("igw-1"))
    assert result.decision == D.ALLOW
    assert result.evidence_ids == sorted(
        {*rtb.fact_evidence["/routes/1"], *association.evidence_ids, *attachment.evidence_ids}
    )


def test_the_main_table_is_used_only_without_an_explicit_association():
    main = table("rtb-main", [LOCAL, route()], main=True)
    private = table("rtb-2", [LOCAL], "subnet-2")
    tables = [main, private]
    assert decision(tables, subnet="subnet-1") == D.ALLOW  # no explicit: main fallback
    assert decision(tables, subnet="subnet-2") == D.DENY  # explicit private table wins


def test_main_table_fallback_needs_an_observed_main_table():
    result = check([table("rtb-2", [LOCAL, route()], "subnet-2")])[1]
    assert result.decision == D.UNKNOWN
    assert result.blockers[0].element == "main_route_table_association"


def test_several_associations_or_main_tables_are_ambiguous():
    several = [table("rtb-1", [route()], "subnet-1"), table("rtb-2", [route()], "subnet-1")]
    assert codes(check(several)[1]) == {B.MISSING_EVIDENCE}
    mains = [table("rtb-1", [route()], main=True), table("rtb-2", [route()], main=True)]
    result = check(mains)[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


# ---- longest prefix -----------------------------------------------------------------


def test_the_longest_matching_prefix_wins_regardless_of_order():
    nat = route("203.0.113.0/24", None, NatGatewayId="nat-1")
    assert check([table("rtb-1", [route(), nat], "subnet-1")])[1].decision == D.UNKNOWN
    blackhole = route("203.0.113.0/24", "igw-1", "blackhole")
    assert decision([table("rtb-1", [blackhole, route()], "subnet-1")]) == D.DENY
    assert decision([table("rtb-1", [route(), blackhole], "subnet-1")]) == D.DENY
    assert (
        decision([table("rtb-1", [blackhole, route()], "subnet-1")], address="198.51.100.1")
        == D.ALLOW
    )


def test_a_shorter_route_does_not_override_a_longer_local_route():
    tables = [table("rtb-1", [LOCAL, route("10.0.0.0/8")], "subnet-1")]
    assert decision(tables, address=INSIDE, reach=LOCAL_REACH) == D.ALLOW
    assert decision(tables, address=INSIDE, reach=IGW) == D.DENY


def test_routes_sharing_the_longest_prefix_are_ambiguous():
    result = check([table("rtb-1", [route(), route(target="igw-2")], "subnet-1")])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


def test_no_matching_route_is_an_implicit_deny():
    index, result = check([table("rtb-1", [LOCAL], "subnet-1")])
    [blocker] = result.blockers
    assert result.decision == D.DENY
    assert (blocker.code, blocker.element) == (B.DENIED, "implicit_deny")
    assert set(index.resource_evidence_ids(resource_id("rtb-1"))) <= set(blocker.evidence_ids)


# ---- targets ------------------------------------------------------------------------


def test_local_and_internet_gateway_routes_are_distinguished():
    tables = [PUBLIC_TABLE]
    assert decision(tables, address=INSIDE, reach=LOCAL_REACH) == D.ALLOW
    assert decision(tables, address=PUBLIC, reach=LOCAL_REACH) == D.DENY
    result = check(tables, address=INSIDE)[1]
    assert result.decision == D.DENY
    assert result.blockers[0].element == "route:/routes/0"


def test_a_blackhole_route_is_denied_even_with_an_unsupported_target():
    nat = route(target=None, state="blackhole", NatGatewayId="nat-1")
    assert decision([table("rtb-1", [nat], "subnet-1")]) == D.DENY
    assert decision([table("rtb-1", [route(state="blackhole")], "subnet-1")]) == D.DENY


def test_an_unrecorded_route_state_is_unknown():
    assert decision([table("rtb-1", [route(state=None)], "subnet-1")]) == D.UNKNOWN
    assert decision([table("rtb-1", [route(state="other")], "subnet-1")]) == D.UNKNOWN


@pytest.mark.parametrize(
    "target",
    [
        {"target": None, "NatGatewayId": "nat-1"},
        {"target": None, "TransitGatewayId": "tgw-1"},
        {"target": None, "VpcPeeringConnectionId": "pcx-1"},
        {"target": "vpce-1"},
    ],
)
def test_unsupported_targets_never_allow(target):
    result = check([table("rtb-1", [LOCAL, route(**target)], "subnet-1")])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}


def test_prefix_list_routes_are_unsupported_and_ipv6_routes_are_ignored():
    prefix = {"DestinationPrefixListId": "pl-1", "GatewayId": "vpce-1", "State": "active"}
    result = check([table("rtb-1", [prefix, route()], "subnet-1")])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}
    ipv6 = {"DestinationIpv6CidrBlock": "::/0", "GatewayId": "igw-1", "State": "active"}
    assert decision([table("rtb-1", [ipv6, LOCAL], "subnet-1")]) == D.DENY


# ---- internet gateways --------------------------------------------------------------


@pytest.mark.parametrize(
    "gateways",
    [[], [gateway(vpc="vpc-9")], [gateway(state="detached")]],
    ids=["unrecorded", "other-vpc", "not-available"],
)
def test_a_gateway_route_needs_an_available_attachment_to_the_vpc(gateways):
    result = check([PUBLIC_TABLE], gateways)[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


# ---- coverage and platform ----------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped"])
def test_limited_route_collection_downgrades_every_decision(status):
    for tables in ([PUBLIC_TABLE], [table("rtb-1", [LOCAL], "subnet-1")]):
        result = check(tables, status=status)[1]
        assert result.decision == D.UNKNOWN
        assert B.COVERAGE_LIMITED in codes(result)


def test_limited_gateway_collection_downgrades_an_allow():
    result = check([PUBLIC_TABLE], gateway_status="failed")[1]
    assert result.decision == D.UNKNOWN
    assert B.COVERAGE_LIMITED in codes(result)


def test_unrecorded_coverage_is_not_interpreted_and_unknown_subnets_are_unknown():
    assert decision([PUBLIC_TABLE], status=None, gateway_status=None) == D.ALLOW
    index = check([PUBLIC_TABLE])[0]
    result = evaluate_subnet_route(index, resource_id("subnet-9"), PUBLIC, reach=IGW)
    assert result.decision == D.UNKNOWN


def test_non_aws_snapshots_return_the_unsupported_platform_result():
    index = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json"))
    result = evaluate_subnet_route(index, "any", PUBLIC, reach=IGW)
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_PLATFORM}


def test_gateway_collection_only_limits_internet_gateway_queries():
    result = check([PUBLIC_TABLE], gateway_status="failed", address=INSIDE, reach=LOCAL_REACH)[1]
    assert result.decision == D.ALLOW
