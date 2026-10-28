from functools import partial

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision, RouteReach
from archdiscov.services import load_snapshot
from archdiscov.services.network_routes import evaluate_subnet_route
from tests.paths import FIXTURES_ROOT
from tests.support.fixture_edits import edited_index
from tests.support.network_snapshot import resource_id
from tests.support.route_data import LOCAL, build, egress_only_gateway, gateway, route, table

D = NetworkDecision
B = NetworkBlockerCode
IGW, LOCAL_REACH = RouteReach.INTERNET_GATEWAY, RouteReach.LOCAL
EIGW = RouteReach.EGRESS_ONLY_INTERNET_GATEWAY
PUBLIC = "203.0.113.9"
INSIDE = "10.0.1.5"
prefix_list_index = partial(edited_index, "endpoints-and-prefix-lists.json")


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
    # The more specific NAT route is selected, and it does not lead to an internet gateway.
    selected = check([table("rtb-1", [route(), nat], "subnet-1")])[1]
    assert (selected.decision, selected.blockers[0].element) == (D.DENY, "route:/routes/1")
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
        {"target": None, "TransitGatewayId": "tgw-1"},
        {"target": None, "VpcPeeringConnectionId": "pcx-1"},
        {"target": "vpce-1"},
    ],
)
def test_unsupported_targets_never_allow(target):
    result = check([table("rtb-1", [LOCAL, route(**target)], "subnet-1")])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}


def test_routes_of_the_other_address_family_are_ignored():
    ipv6 = {"DestinationIpv6CidrBlock": "::/0", "GatewayId": "igw-1", "State": "active"}
    assert decision([table("rtb-1", [ipv6, LOCAL], "subnet-1")]) == D.DENY
    assert decision([PUBLIC_TABLE], address="2001:db8::1") == D.DENY


# ---- prefix-list routes -------------------------------------------------------------

S3_ENTRIES = {"pl-s3": {"entries": [{"cidr": "52.218.0.0/17"}]}}
# rtb-svc's routes: local, the endpoint's pl-s3, and pl-office to a transit gateway.
ROUTES = [
    route.model_dump(mode="json")
    for route in prefix_list_index().get_resource(resource_id("rtb-svc")).routes
]


def service_route(index, address="198.51.100.7"):
    return evaluate_subnet_route(index, resource_id("subnet-svc"), address, reach=LOCAL_REACH)


def test_a_prefix_list_route_is_selected_through_its_recorded_entries():
    index = prefix_list_index(S3_ENTRIES)
    # The selected route leads to a transit gateway, which step 53 decides.
    [blocker] = service_route(index).blockers
    assert (blocker.code, blocker.element) == (B.UNSUPPORTED_ELEMENT, "route:/routes/2")
    assert service_route(index, "192.0.2.1").blockers[0].element == "implicit_deny"
    blackhole = {"rtb-svc": {"routes": [*ROUTES[:2], {**ROUTES[2], "state": "blackhole"}]}}
    denied = service_route(prefix_list_index({**S3_ENTRIES, **blackhole}))
    assert denied.decision == D.DENY
    assert {"pl-office-entry-0", "rtb-office"} <= set(denied.blockers[0].evidence_ids)


def test_an_unrecorded_list_or_a_tie_at_the_longest_prefix_is_unknown_with_its_reason():
    office = "198.51.100.0/24"
    static = {"destination": office, "target_kind": "local", "state": "active"}
    cases = {
        "entries of prefix list pl-s3 are not recorded": {},
        "whether the route is static or propagated": {
            **S3_ENTRIES,
            "rtb-svc": {"routes": [*ROUTES, static]},
        },
        "at random": {"pl-s3": {"entries": [{"cidr": office}]}},
    }
    for reason, changes in cases.items():
        result = service_route(prefix_list_index(changes))
        [blocker] = result.blockers
        assert (result.decision, blocker.code) == (D.UNKNOWN, B.MISSING_EVIDENCE)
        assert reason in blocker.message


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


# ---- IPv6 and egress-only internet gateways -----------------------------------------


def test_dual_stack_ipv6_routes_reach_the_internet_and_egress_only_gateways():
    index = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "dual-stack.json"))

    def evaluate(subnet, address, reach):
        return evaluate_subnet_route(index, resource_id(subnet), address, reach=reach)

    public = evaluate("subnet-dual-public", "2001:db8:ffff::1", IGW)
    assert public.decision == D.ALLOW
    assert public.evidence_ids == [
        "igw-attachment",
        "rtb-dual-public-association",
        "rtb-dual-public-ipv6-default",
    ]
    private = evaluate("subnet-dual-private", "2001:db8:ffff::1", EIGW)
    assert private.decision == D.ALLOW
    assert "eigw-attachment" in private.evidence_ids
    # An egress-only gateway is not an internet gateway, so inbound exposure never accepts it.
    assert evaluate("subnet-dual-private", "2001:db8:ffff::1", IGW).decision == D.DENY


@pytest.mark.parametrize(
    "gateways",
    [[], [egress_only_gateway(state="detached")]],
    ids=["unrecorded", "detached"],
)
def test_an_egress_only_route_needs_an_attached_gateway(gateways):
    ipv6 = {
        "DestinationIpv6CidrBlock": "::/0",
        "EgressOnlyInternetGatewayId": "eigw-1",
        "State": "active",
    }
    tables = [table("rtb-1", [ipv6], "subnet-1")]
    result = check(tables, address="2001:db8::1", reach=EIGW, egress_only_gateways=gateways)[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}
    attached = [egress_only_gateway()]
    assert decision(tables, address="2001:db8::1", reach=EIGW, egress_only_gateways=attached) == (
        D.ALLOW
    )


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
