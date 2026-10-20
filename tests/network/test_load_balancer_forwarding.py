import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision
from archdiscov.services import load_snapshot
from archdiscov.services.network_load_balancers import (
    evaluate_load_balancer_forwarding,
    evaluate_load_balancer_scheme,
)
from tests.paths import FIXTURES_ROOT
from tests.support.elbv2_snapshot import ec2_roots
from tests.support.lb_data import GROUP, LB, LISTENER, RULE, build, resource_id, responses

D = NetworkDecision
B = NetworkBlockerCode
NO_RULES = {"Rules": []}
REDIRECT = {
    "Type": "redirect",
    "Order": 1,
    "RedirectConfig": {"Protocol": "HTTPS", "Port": "443", "StatusCode": "HTTP_301"},
}


def data(**changes):
    value = responses()
    for key, change in changes.items():
        value[key] = change
    return value


def forwarding(data=None, *, port=80, protocol="tcp", **kwargs):
    index = GraphIndex(build(data, **kwargs))
    return index, evaluate_load_balancer_forwarding(
        index, resource_id(LB), protocol=protocol, port=port
    )


def scheme(data=None, **kwargs):
    index = GraphIndex(build(data, **kwargs))
    return index, evaluate_load_balancer_scheme(index, resource_id(LB))


def codes(result):
    return {blocker.code for blocker in result.blockers}


def with_load_balancer(**changes):
    value = responses()
    value["describe_load_balancers"]["LoadBalancers"][0].update(changes)
    return value


# ---- scheme -------------------------------------------------------------------------


def test_internet_facing_allows_and_internal_denies_citing_the_scheme():
    index, result = scheme(with_load_balancer(Scheme="internet-facing"))
    balancer = index.get_resource(resource_id(LB))
    assert result.decision == D.ALLOW
    assert result.evidence_ids == sorted(balancer.fact_evidence["/scheme"])
    internal = scheme()[1]
    assert internal.decision == D.DENY
    assert internal.blockers[0].code == B.DENIED


def test_an_internet_facing_balancer_without_a_public_ipv4_address_is_unknown():
    result = scheme(
        with_load_balancer(Scheme="internet-facing", IpAddressType="dualstack-without-public-ipv4")
    )[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}


def test_an_unrecorded_scheme_is_unknown():
    value = responses()
    del value["describe_load_balancers"]["LoadBalancers"][0]["Scheme"]
    result = scheme(value)[1]
    assert result.decision == D.UNKNOWN
    # normalization also records the malformed value as limited coverage
    assert codes(result) == {B.MISSING_EVIDENCE, B.COVERAGE_LIMITED}


def test_limited_load_balancer_collection_limits_the_scheme():
    result = scheme(limited="DescribeLoadBalancers")[1]
    assert result.decision == D.UNKNOWN
    assert B.COVERAGE_LIMITED in codes(result)


# ---- listeners ----------------------------------------------------------------------


def test_a_forwarded_listener_allows_citing_the_listener_forward_and_target():
    index, result = forwarding()
    listener = index.get_resource(resource_id(LISTENER))
    group = index.get_resource(resource_id(GROUP))
    assert result.decision == D.ALLOW
    assert set(listener.fact_evidence["/port"]) <= set(result.evidence_ids)
    assert set(listener.fact_evidence["/protocol"]) <= set(result.evidence_ids)
    assert set(listener.fact_evidence["/actions/0/forward_groups/0/arn"]) <= set(
        result.evidence_ids
    )
    assert set(group.fact_evidence["/targets/0/id"]) <= set(result.evidence_ids)
    assert set(group.fact_evidence["/targets/0/health_state"]) <= set(result.evidence_ids)


@pytest.mark.parametrize(
    ("port", "protocol", "expected"),
    [(80, "tcp", D.ALLOW), (80, "6", D.ALLOW), (81, "tcp", D.DENY), (80, "udp", D.DENY)],
)
def test_the_listener_port_and_protocol_must_match(port, protocol, expected):
    assert forwarding(port=port, protocol=protocol)[1].decision == expected


def test_a_denied_query_cites_the_load_balancer_evidence():
    index, result = forwarding(port=81)
    [blocker] = result.blockers
    assert (blocker.code, blocker.element) == (B.DENIED, "implicit_deny")
    assert set(index.resource_evidence_ids(resource_id(LB))) <= set(blocker.evidence_ids)


def test_an_unsupported_query_protocol_is_unknown():
    result = forwarding(protocol="gre")[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}


# ---- actions ------------------------------------------------------------------------


def redirecting():
    value = data(describe_rules=NO_RULES)
    value["describe_listeners"]["Listeners"][0]["DefaultActions"] = [REDIRECT]
    return value


def test_unsupported_listener_actions_are_unknown():
    result = forwarding(redirecting())[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}


def test_a_conditional_rule_that_forwards_can_still_allow():
    value = redirecting()
    value["describe_rules"] = responses()["describe_rules"]
    assert forwarding(value)[1].decision == D.ALLOW


def test_default_rules_are_not_counted_as_alternatives():
    value = data(describe_rules=NO_RULES)
    value["describe_rules"] = {
        "Rules": [
            {
                "RuleArn": RULE,
                "Priority": "default",
                "IsDefault": True,
                "Conditions": [],
                "Actions": [REDIRECT],
            }
        ]
    }
    assert forwarding(value)[1].decision == D.ALLOW


def test_a_forward_action_to_a_zero_weight_group_forwards_nothing():
    value = responses()
    group = value["describe_listeners"]["Listeners"][0]["DefaultActions"][0]["ForwardConfig"]
    group["TargetGroups"][0]["Weight"] = 0
    value["describe_rules"] = NO_RULES
    result = forwarding(value)[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


# ---- targets ------------------------------------------------------------------------


def test_a_target_group_without_registered_targets_denies():
    value = data(describe_target_health={"TargetHealthDescriptions": []}, describe_rules=NO_RULES)
    index, result = forwarding(value)
    assert result.decision == D.DENY
    assert result.blockers[0].resource_id == resource_id(GROUP)


def test_unhealthy_targets_still_count_because_health_is_not_interpreted():
    health = responses()["describe_target_health"]
    assert health["TargetHealthDescriptions"][0]["TargetHealth"]["State"] == "unhealthy"
    assert forwarding()[1].decision == D.ALLOW


def test_a_target_that_is_not_a_recorded_resource_is_unknown():
    roots = ec2_roots()
    subnet = roots[0].children[0]
    subnet.children.clear()
    result = forwarding(roots=roots, data=data(describe_rules=NO_RULES))[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


def test_ip_targets_are_not_resolved_to_resources():
    value = data(describe_rules=NO_RULES)
    value["describe_target_groups"]["TargetGroups"][0]["TargetType"] = "ip"
    value["describe_target_health"]["TargetHealthDescriptions"][0]["Target"]["Id"] = "10.0.0.7"
    result = forwarding(value)[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


def test_unrecorded_target_health_is_unknown():
    value = data(describe_rules=NO_RULES)
    value["describe_target_health"]["TargetHealthDescriptions"][0]["TargetHealth"] = {}
    result = forwarding(value)[1]
    assert result.decision == D.UNKNOWN
    # normalization also records the malformed value as limited coverage
    assert codes(result) == {B.MISSING_EVIDENCE, B.COVERAGE_LIMITED}


@pytest.mark.parametrize(
    "operation",
    ["DescribeListeners", "DescribeRules", "DescribeTargetGroups", "DescribeTargetHealth"],
)
def test_limited_collection_of_any_forwarding_operation_gives_unknown(operation):
    for port in (80, 81):
        result = forwarding(port=port, limited=operation)[1]
        assert result.decision == D.UNKNOWN
        assert B.COVERAGE_LIMITED in codes(result)


# ---- platform and lookup ------------------------------------------------------------


def test_unrecorded_balancers_and_non_aws_snapshots_are_unknown():
    index = GraphIndex(build())
    missing = evaluate_load_balancer_forwarding(index, resource_id("nope"), protocol="tcp", port=80)
    assert codes(missing) == {B.MISSING_EVIDENCE}
    azure = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json"))
    for result in (
        evaluate_load_balancer_forwarding(azure, "any", protocol="tcp", port=80),
        evaluate_load_balancer_scheme(azure, "any"),
    ):
        assert codes(result) == {B.UNSUPPORTED_PLATFORM}


def test_a_listener_on_another_port_does_not_make_the_query_unknown():
    value = responses()
    value["describe_listeners"]["Listeners"][0]["Protocol"] = "GENEVE"
    assert forwarding(value, port=81)[1].decision == D.DENY
    assert forwarding(value, port=80)[1].decision == D.UNKNOWN


def test_evaluate_listener_only_asks_whether_a_listener_accepts_the_traffic():
    from archdiscov.services.network_load_balancers import evaluate_listener

    index = GraphIndex(build())
    for port, protocol, expected in (
        (80, "tcp", D.ALLOW),
        (81, "tcp", D.DENY),
        (80, "udp", D.DENY),
    ):
        result = evaluate_listener(index, resource_id(LB), protocol=protocol, port=port)
        assert result.decision == expected
    allowed = evaluate_listener(index, resource_id(LB), protocol="tcp", port=80)
    listener = next(r for r in index.resources() if r.resource_type.endswith("Listener"))
    assert set(listener.fact_evidence["/port"]) <= set(allowed.evidence_ids)


def test_a_target_walk_can_relax_health_and_add_a_per_target_check():
    from archdiscov.services.network_decisions import missing
    from archdiscov.services.network_load_balancers import TargetWalk

    value = responses()
    value["describe_target_health"]["TargetHealthDescriptions"][0]["TargetHealth"] = {}
    index = GraphIndex(build(value))
    seen = []

    def check(group, target, resource):
        seen.append((group.id, target.id, resource.id))
        return missing("Backend not admitted.", element="backend")

    relaxed = TargetWalk(check=check, require_health=False)
    result = evaluate_load_balancer_forwarding(
        index, resource_id(LB), protocol="tcp", port=80, walk=relaxed
    )
    assert seen and result.decision == D.UNKNOWN
    assert codes(result) >= {B.MISSING_EVIDENCE}
