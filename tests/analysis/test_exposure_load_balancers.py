import pytest

from archdiscov.graph import GraphIndex
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.enums import (
    ExposureElement,
    ExposureEntryKind,
    ExposureProtocol,
    NetworkDecision,
    TargetHealthState,
)
from archdiscov.services.exposure_load_balancers import (
    evaluate_load_balancer_entry,
    load_balancer_entries,
)
from tests.support.elbv2_data import LISTENER, NLB
from tests.support.exposure_data import sg_rule
from tests.support.lb_exposure_data import build, ec2_items, responses

D = NetworkDecision
X = ExposureElement
TCP, UDP = ExposureProtocol.TCP, ExposureProtocol.UDP
FULL_PATH = [e for e in ExposureElement if e != ExposureElement.PUBLIC_ADDRESS]


def public(**changes):
    data = responses()
    data["describe_load_balancers"]["LoadBalancers"][0].update(Scheme="internet-facing", **changes)
    return data


def evaluate(data=None, *, max_depth=3, **options):
    index = GraphIndex(build(data or public(), **options))
    [balancer] = load_balancer_entries(index)
    return index, evaluate_load_balancer_entry(index, balancer, max_depth)


def by_type(index, resource_type):
    return next(r for r in index.resources() if r.resource_type == resource_type)


def network_balancer(**changes):
    """The public balancer as a network load balancer (its ARN names the kind)."""
    data = public(Type="network", LoadBalancerArn=NLB, **changes)
    listener = data["describe_listeners"]["Listeners"][0]
    listener.update(
        LoadBalancerArn=NLB, Protocol="TCP", ListenerArn=LISTENER.replace("app/web", "net/tcp")
    )
    data["describe_rules"] = {"Rules": []}
    return data


def decisions(evaluation):
    return {item.element: item.decision for item in evaluation.elements}


def codes(evaluation):
    return [item.code for item in evaluation.limitations]


def blockers(evaluation, element):
    return next(item.blocker_codes for item in evaluation.elements if item.element == element)


# ---- public load balancer with a private backend ------------------------------------


def test_a_public_load_balancer_exposes_its_listener_and_reaches_a_private_backend():
    _, [found] = evaluate()
    assert found.kind == ExposureEntryKind.LOAD_BALANCER
    assert (found.protocol, found.start, found.end, found.decision) == (TCP, 80, 80, D.ALLOW)
    assert [item.element for item in found.elements] == FULL_PATH
    assert {item.decision for item in found.elements} == {D.ALLOW}
    assert [t.identity.physical_id for t in found.targets] == ["i-web"]
    assert found.entry.identity.physical_id.endswith("loadbalancer/app/web/1234567890abcdef")


def test_the_evaluation_cites_the_listener_the_subnets_and_the_targets():
    index, [found] = evaluate()
    listener = next(
        r for r in index.resources() if r.resource_type == "AWS::ElasticLoadBalancingV2::Listener"
    )
    group = next(
        r
        for r in index.resources()
        if r.resource_type == "AWS::ElasticLoadBalancingV2::TargetGroup"
    )
    assert set(listener.fact_evidence["/port"]) <= set(found.evidence_ids)
    assert set(group.fact_evidence["/targets/0/id"]) <= set(found.evidence_ids)
    assert list(found.evidence_ids) == sorted(found.evidence_ids)


# ---- internal and unscheduled balancers ---------------------------------------------


def test_an_internal_load_balancer_is_a_cited_denial_on_the_scheme():
    _, [found] = evaluate(responses())
    assert (found.start, found.end, found.decision) == (1, 65535, D.DENY)
    assert decisions(found) == {X.SCHEME: D.DENY}
    assert "denied" in codes(found)


def test_a_balancer_without_listeners_denies_at_the_listener():
    data = public()
    data["describe_listeners"] = {"Listeners": []}
    data["describe_rules"] = {"Rules": []}
    _, [found] = evaluate(data)
    assert decisions(found) == {X.SCHEME: D.ALLOW, X.LISTENER: D.DENY}
    assert found.decision == D.DENY


# ---- blocked paths ------------------------------------------------------------------


def test_a_security_group_that_does_not_admit_the_internet_on_the_listener_port_denies():
    items = ec2_items()
    items[Op.DESCRIBE_SECURITY_GROUP_RULES] = [items[Op.DESCRIBE_SECURITY_GROUP_RULES][1]]
    _, [found] = evaluate(ec2=items)
    assert decisions(found)[X.SECURITY_GROUP] == D.DENY
    assert found.decision == D.DENY


def test_a_backend_that_does_not_admit_the_load_balancers_group_denies_the_target():
    _, [found] = evaluate(ec2=ec2_items(backend_rules=[]))
    assert decisions(found)[X.TARGET] == D.DENY
    assert found.decision == D.DENY
    assert [t.identity.physical_id for t in found.targets] == ["i-web"]


def test_a_missing_default_route_denies_and_missing_route_records_are_unknown():
    _, [found] = evaluate(ec2=ec2_items(public_route=False))
    assert decisions(found)[X.ROUTE] == D.DENY
    items = ec2_items()
    del items[Op.DESCRIBE_ROUTE_TABLES]
    _, [found] = evaluate(ec2=items)
    assert decisions(found)[X.ROUTE] == D.UNKNOWN
    assert blockers(found, X.ROUTE) == ["missing_evidence"]
    assert found.decision == D.UNKNOWN


def test_missing_acl_records_are_unknown():
    _, [found] = evaluate(ec2=ec2_items(acl=False))
    assert decisions(found)[X.NACL] == D.UNKNOWN


# ---- targets ------------------------------------------------------------------------


def test_target_health_is_observed_but_never_changes_the_decision():
    data = public()
    data["describe_target_health"]["TargetHealthDescriptions"][0]["TargetHealth"] = {
        "State": "unhealthy"
    }
    _, [found] = evaluate(data)
    assert found.decision == D.ALLOW
    assert found.observed_target_health == (TargetHealthState.UNHEALTHY,)


def test_ip_and_unrecorded_targets_are_unresolved_and_unknown():
    data = public()
    data["describe_target_groups"]["TargetGroups"][0]["TargetType"] = "ip"
    data["describe_target_health"]["TargetHealthDescriptions"][0]["Target"]["Id"] = "10.0.2.9"
    _, [found] = evaluate(data)
    assert decisions(found)[X.TARGET] == D.UNKNOWN
    assert found.targets == ()
    items = ec2_items()
    items[Op.DESCRIBE_INSTANCES] = [{"Instances": []}]
    _, [found] = evaluate(ec2=items)
    assert decisions(found)[X.TARGET] == D.UNKNOWN


def test_unsupported_listener_actions_make_the_target_unknown():
    data = public()
    redirect = {
        "Type": "redirect",
        "Order": 1,
        "RedirectConfig": {"Protocol": "HTTPS", "Port": "443", "StatusCode": "HTTP_301"},
    }
    data["describe_listeners"]["Listeners"][0]["DefaultActions"] = [redirect]
    data["describe_rules"] = {"Rules": []}
    _, [found] = evaluate(data)
    assert decisions(found)[X.LISTENER] == D.ALLOW
    assert decisions(found)[X.TARGET] == D.UNKNOWN
    assert blockers(found, X.TARGET) == ["unsupported_element"]


def test_a_target_group_without_registered_targets_denies():
    data = public()
    data["describe_target_health"] = {"TargetHealthDescriptions": []}
    data["describe_rules"] = {"Rules": []}
    _, [found] = evaluate(data)
    assert decisions(found)[X.TARGET] == D.DENY
    assert found.targets == ()


def test_backends_are_followed_only_within_max_depth():
    items = ec2_items(backend_rules=[])
    _, [shallow] = evaluate(ec2=items, max_depth=1)
    assert decisions(shallow)[X.TARGET] == D.ALLOW  # the backend's groups are not examined
    assert shallow.targets == () and shallow.observed_target_health == ()
    _, [deep] = evaluate(ec2=items, max_depth=2)
    assert decisions(deep)[X.TARGET] == D.DENY


# ---- listeners and protocols --------------------------------------------------------


def test_a_tcp_udp_listener_is_evaluated_for_each_protocol():
    data = public()
    data["describe_listeners"]["Listeners"][0]["Protocol"] = "TCP_UDP"
    _, found = evaluate(data)
    assert [(item.protocol, item.start, item.end) for item in found] == [
        (TCP, 80, 80),
        (UDP, 80, 80),
    ]
    assert decisions(found[0])[X.SECURITY_GROUP] == D.ALLOW
    assert decisions(found[1])[X.SECURITY_GROUP] == D.DENY  # the open rule is tcp only
    assert found[1].decision == D.DENY


def test_network_balancers_without_security_groups_omit_that_element():
    data = network_balancer(SecurityGroups=[])
    open_backend = ec2_items(backend_rules=[sg_rule("sg-backend", 8080)])  # no group to reference
    _, [found] = evaluate(data, ec2=open_backend)
    assert X.SECURITY_GROUP not in decisions(found)
    assert found.decision == D.ALLOW


def test_an_unrecorded_security_group_list_is_unknown():
    data = network_balancer()
    del data["describe_load_balancers"]["LoadBalancers"][0]["SecurityGroups"]
    _, [found] = evaluate(data)
    assert decisions(found)[X.SECURITY_GROUP] == D.UNKNOWN


# ---- collection ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation", ["DescribeLoadBalancers", "DescribeListeners", "DescribeTargetGroups"]
)
def test_limited_balancer_collection_makes_the_decision_unknown(operation):
    _, [found] = evaluate(limited=operation)
    assert found.decision == D.UNKNOWN
    assert "coverage_limited" in codes(found)


def test_evaluation_is_deterministic():
    first = evaluate()[1]
    second = evaluate()[1]
    assert [(e.start, e.decision, e.evidence_ids) for e in first] == [
        (e.start, e.decision, e.evidence_ids) for e in second
    ]


def test_the_backends_security_group_membership_is_cited():
    index, [found] = evaluate()
    [backend] = found.targets
    edges = index.outgoing_edges(backend.id, attachment_kind="security_group_membership")
    assert edges and set(edges[0].evidence_ids) <= set(found.evidence_ids)
