"""Primitives composed on small topologies, and run across every AWS fixture."""

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.enums import (
    NetworkBlockerCode,
    NetworkDecision,
    ResourceType,
    RouteReach,
    RuleDirection,
)
from archdiscov.services import load_snapshot
from archdiscov.services.network_acls import evaluate_network_acl
from archdiscov.services.network_addressing import evaluate_public_address
from archdiscov.services.network_composition import subnet_routes
from archdiscov.services.network_decisions import all_of
from archdiscov.services.network_load_balancers import (
    evaluate_load_balancer_forwarding,
    evaluate_load_balancer_scheme,
)
from archdiscov.services.network_routes import evaluate_subnet_route
from archdiscov.services.network_security_groups import Peer, evaluate_security_group
from tests.paths import FIXTURES_ROOT
from tests.support.elbv2_data import LB, responses
from tests.support.elbv2_snapshot import build as build_lb
from tests.support.elbv2_snapshot import resource_id as lb_id
from tests.support.fixture_edits import edited_index
from tests.support.nacl_data import acl, entry
from tests.support.network_data import ENI, FULL, ROUTE_TABLE, SG_RULES
from tests.support.network_snapshot import build_network, resource_id

D = NetworkDecision
B = NetworkBlockerCode
INGRESS, EGRESS = RuleDirection.INGRESS, RuleDirection.EGRESS
CLIENT = "203.0.113.9"
EPHEMERAL = 49152
ALL_COVERED = dict.fromkeys(
    (
        "DescribeRouteTables",
        "DescribeInternetGateways",
        "DescribeNetworkAcls",
        "DescribeSecurityGroups",
        "DescribeSecurityGroupRules",
        "DescribeNetworkInterfaces",
    ),
    "success",
)
OPEN_ACL = acl(
    "acl-1",
    [entry(100, ports=(443, 443)), entry(100, egress=True, ports=(1024, 65535))],
    "subnet-1",
)

EGRESS_RULE = {
    "SecurityGroupRuleId": "sgr-2",
    "GroupId": "sg-1",
    "IsEgress": True,
    "IpProtocol": "-1",
    "CidrIpv4": "0.0.0.0/0",
}


def topology(*, acl_entries=None, covered=None):
    """A public subnet whose ENI sits behind sg-1 (HTTPS from anywhere, all egress)."""
    items = {
        **FULL,
        Op.DESCRIBE_ROUTE_TABLES: [
            {**ROUTE_TABLE, "Associations": [{"SubnetId": "subnet-1", "Main": False}]}
        ],
        Op.DESCRIBE_NETWORK_ACLS: [
            OPEN_ACL if acl_entries is None else acl("acl-1", acl_entries, "subnet-1")
        ],
        Op.DESCRIBE_NETWORK_INTERFACES: [ENI],
        Op.DESCRIBE_SECURITY_GROUP_RULES: [*SG_RULES, EGRESS_RULE],
    }
    return GraphIndex(build_network(items, covered={**ALL_COVERED, **(covered or {})}))


def inbound_https(index, *, port=443):
    """Every element of the inbound request and its return path, composed explicitly."""
    subnet, eni, group = resource_id("subnet-1"), resource_id("eni-1"), resource_id("sg-1")
    peer = Peer.address(CLIENT, ())
    request_and_reply = ((INGRESS, port), (EGRESS, EPHEMERAL))
    return all_of(
        [
            evaluate_public_address(index, eni),
            evaluate_subnet_route(index, subnet, CLIENT, reach=RouteReach.INTERNET_GATEWAY),
            *(
                evaluate_network_acl(index, subnet, direction, CLIENT, protocol="tcp", port=p)
                for direction, p in request_and_reply
            ),
            *(
                evaluate_security_group(index, group, direction, peer, protocol="tcp", port=p)
                for direction, p in request_and_reply
            ),
        ]
    )


def codes(result):
    return {blocker.code for blocker in result.blockers}


# ---- composed topologies ------------------------------------------------------------


def test_every_element_allowing_composes_to_a_cited_allow():
    result = inbound_https(topology())
    assert result.decision == D.ALLOW
    assert result.evidence_ids
    assert result == inbound_https(topology())


def test_a_nacl_denial_dominates_the_composition():
    deny_first = [entry(90, "deny"), entry(100), entry(100, egress=True, ports=(1024, 65535))]
    result = inbound_https(topology(acl_entries=deny_first))
    assert result.decision == D.DENY
    assert codes(result) == {B.DENIED}


def test_a_missing_return_path_denies_because_nacls_are_stateless():
    result = inbound_https(topology(acl_entries=[entry(100)]))
    assert result.decision == D.DENY


def test_a_closed_security_group_port_denies():
    assert inbound_https(topology(), port=22).decision == D.DENY


# Which statuses limit an element is each element's own test; one limited status per element
# shows that the composition carries it.
@pytest.mark.parametrize("operation", ["DescribeRouteTables", "DescribeNetworkAcls"])
def test_failed_route_or_nacl_collection_downgrades_the_composition_to_unknown(operation):
    result = inbound_https(topology(covered={operation: "failed"}))
    assert result.decision == D.UNKNOWN
    assert B.COVERAGE_LIMITED in codes(result)
    assert operation in {blocker.element for blocker in result.blockers}


def test_a_recorded_denial_still_dominates_an_unknown_element():
    index = topology(covered={"DescribeNetworkAcls": "failed"})
    result = inbound_https(index, port=22)
    assert result.decision == D.DENY
    assert codes(result) == {B.DENIED}


def test_unrecorded_elements_never_allow():
    index = topology()
    result = all_of(
        [
            evaluate_public_address(index, resource_id("eni-1")),
            evaluate_network_acl(
                index, resource_id("subnet-9"), INGRESS, CLIENT, protocol="tcp", port=443
            ),
        ]
    )
    assert result.decision == D.UNKNOWN


def test_load_balancer_scheme_and_forwarding_compose():
    def exposed(scheme):
        value = responses()
        value["describe_load_balancers"]["LoadBalancers"][0]["Scheme"] = scheme
        index = GraphIndex(build_lb(value))
        return all_of(
            [
                evaluate_load_balancer_scheme(index, lb_id(LB)),
                evaluate_load_balancer_forwarding(index, lb_id(LB), protocol="tcp", port=80),
            ]
        )

    assert exposed("internet-facing").decision == D.ALLOW
    assert exposed("internal").decision == D.DENY


# ---- every AWS fixture and schema ---------------------------------------------------

# Fixture -> the number of primitive calls its resources must produce (zero: no network
# resources, but the primitives must still not fail).
AWS_FIXTURES = {
    "aws.json": 0,
    "network.json": 11,
    "load-balancing.json": 2,
    "hierarchy.json": 7,
    "iam.json": 0,
    "dependency-targets.json": 7,
    "partial-coverage.json": 0,
}


def run_primitives(index, resource):
    match resource.resource_type:
        case ResourceType.SECURITY_GROUP:
            for direction in (INGRESS, EGRESS):
                yield evaluate_security_group(
                    index,
                    resource.id,
                    direction,
                    Peer.address("10.0.0.1"),
                    protocol="tcp",
                    port=443,
                )
        case ResourceType.SUBNET:
            yield evaluate_network_acl(
                index, resource.id, INGRESS, "10.0.0.1", protocol="tcp", port=443
            )
            for reach in RouteReach:
                yield evaluate_subnet_route(index, resource.id, "10.0.0.1", reach=reach)
        case ResourceType.NETWORK_INTERFACE | ResourceType.EC2_INSTANCE:
            yield evaluate_public_address(index, resource.id)
        case ResourceType.ELBV2_LOAD_BALANCER:
            yield evaluate_load_balancer_scheme(index, resource.id)
            yield evaluate_load_balancer_forwarding(index, resource.id, protocol="tcp", port=80)


@pytest.mark.parametrize(("name", "calls"), AWS_FIXTURES.items())
def test_every_primitive_returns_a_decision_for_every_resource_of_each_aws_fixture(name, calls):
    index = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / name))
    results = [
        result for resource in index.resources() for result in run_primitives(index, resource)
    ]
    assert len(results) == calls
    assert all(B.UNSUPPORTED_PLATFORM not in codes(result) for result in results)


def test_the_network_fixture_exercises_decisions_beyond_unknown():
    index = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "network.json"))
    decisions = {
        result.decision
        for resource in index.resources()
        for result in run_primitives(index, resource)
    }
    assert len(decisions) > 1


def test_every_primitive_reports_the_unsupported_platform_for_azure():
    index = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json"))
    results = [
        evaluate_security_group(
            index, "x", INGRESS, Peer.address("10.0.0.1"), protocol="tcp", port=443
        ),
        evaluate_network_acl(index, "x", INGRESS, "10.0.0.1", protocol="tcp", port=443),
        evaluate_subnet_route(index, "x", "10.0.0.1", reach=RouteReach.LOCAL),
        evaluate_public_address(index, "x"),
        evaluate_load_balancer_scheme(index, "x"),
        evaluate_load_balancer_forwarding(index, "x", protocol="tcp", port=80),
    ]
    for result in results:
        assert result.decision == D.UNKNOWN
        assert codes(result) == {B.UNSUPPORTED_PLATFORM}
        assert "Analyze an AWS snapshot" in result.blockers[0].message


# ---- routes between two subnets -------------------------------------------------------

# vpc-data's table with only its local route: no way back to vpc-app.
NO_RETURN = {
    "rtb-data": {
        "routes": [{"destination": "10.3.0.0/16", "target_kind": "local", "state": "active"}],
        "fact_evidence": {},
    }
}


def app_and_data(index, destination="subnet-data"):
    return subnet_routes(index, resource_id("subnet-app"), resource_id(destination))


@pytest.mark.parametrize("fixture", ["vpc-peering.json", "transit-gateway.json"])
def test_subnets_in_two_vpcs_route_both_ways_citing_every_hop(fixture):
    result = app_and_data(edited_index(fixture))
    assert result.decision == D.ALLOW
    assert {"rtb-app-route-1", "rtb-data-route-1"} <= set(result.evidence_ids)


def test_subnets_in_one_vpc_give_the_local_route_result():
    index = edited_index("vpc-peering.json")
    local = evaluate_subnet_route(
        index, resource_id("subnet-app"), "10.2.1.0/24", reach=RouteReach.LOCAL
    )
    result = app_and_data(index, "subnet-app")
    assert (result.decision, result.evidence_ids) == (D.ALLOW, local.evidence_ids)


def test_a_missing_return_route_denies():
    # How each connection's own state decides is its handler's test (test_vpc_peering.py).
    assert app_and_data(edited_index("transit-gateway.json", NO_RETURN)).decision == D.DENY
