"""Primitives accept a CIDR that bounds an address which is not known exactly."""

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision, RouteReach, RuleDirection
from archdiscov.services.network_acls import evaluate_network_acl
from archdiscov.services.network_routes import evaluate_subnet_route
from archdiscov.services.network_security_groups import Peer, evaluate_security_group
from tests.support.nacl_data import acl, entry
from tests.support.nacl_data import build as build_acl
from tests.support.network_snapshot import resource_id
from tests.support.route_data import LOCAL, route, table
from tests.support.route_data import build as build_routes
from tests.support.sg_rule_data import build as build_groups
from tests.support.sg_rule_data import group_id, rule

D = NetworkDecision
B = NetworkBlockerCode


def nacl(cidr, address):
    index = GraphIndex(build_acl([acl("acl-1", [entry(100, CidrBlock=cidr)], "subnet-1")]))
    return evaluate_network_acl(
        index, resource_id("subnet-1"), RuleDirection.INGRESS, address, protocol="tcp", port=443
    )


def test_a_nacl_entry_covering_the_whole_range_matches_and_a_disjoint_one_does_not():
    assert nacl("10.0.0.0/16", "10.0.2.0/24").decision == D.ALLOW
    assert nacl("192.168.0.0/16", "10.0.2.0/24").decision == D.DENY


def test_a_nacl_entry_covering_part_of_the_range_is_unknown():
    result = nacl("10.0.2.0/25", "10.0.2.0/24")
    assert result.decision == D.UNKNOWN
    assert {b.code for b in result.blockers} == {B.MISSING_EVIDENCE}


def test_a_single_address_never_partially_overlaps():
    assert nacl("10.0.2.0/25", "10.0.2.9").decision == D.ALLOW
    assert nacl("10.0.2.0/25", "10.0.2.200").decision == D.DENY


def group(cidr, peer):
    index = GraphIndex(build_groups({"sg-app": [rule(cidr=cidr)]}))
    return evaluate_security_group(
        index, group_id("sg-app"), RuleDirection.INGRESS, peer, protocol="tcp", port=443
    )


def test_a_bounded_peer_distinguishes_covered_partial_and_disjoint_rules():
    assert group("10.0.0.0/16", Peer.within("10.0.2.0/24", ())).decision == D.ALLOW
    assert group("10.0.2.0/25", Peer.within("10.0.2.0/24", ())).decision == D.UNKNOWN
    assert group("192.168.0.0/16", Peer.within("10.0.2.0/24", ())).decision == D.DENY


def test_an_exact_cidr_peer_still_needs_to_be_fully_contained():
    assert group("10.0.2.0/25", Peer.cidr("10.0.2.0/24", ())).decision == D.DENY


def local_route(routes, address):
    index = GraphIndex(build_routes([table("rtb-1", routes, "subnet-1")], []))
    return evaluate_subnet_route(index, resource_id("subnet-1"), address, reach=RouteReach.LOCAL)


def test_a_route_covering_the_whole_range_decides_a_range_query():
    assert local_route([LOCAL], "10.0.2.0/24").decision == D.ALLOW


def test_a_more_specific_route_inside_the_range_makes_it_unknown():
    narrower = route("10.0.2.0/25", "igw-1")
    result = local_route([LOCAL, narrower], "10.0.2.0/24")
    assert result.decision == D.UNKNOWN
    assert local_route([LOCAL, narrower], "10.0.2.200").decision == D.ALLOW
