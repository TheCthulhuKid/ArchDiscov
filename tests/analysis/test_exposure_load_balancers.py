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
TCP = ExposureProtocol.TCP
FULL_PATH = [e for e in ExposureElement if e != ExposureElement.PUBLIC_ADDRESS]
NO_RULES = {"Rules": []}
REDIRECT = {
    "Type": "redirect",
    "Order": 1,
    "RedirectConfig": {"Protocol": "HTTPS", "Port": "443", "StatusCode": "HTTP_301"},
}


def public(**replaced):
    """The internet-facing balancer's API data, with whole responses replaced."""
    data = responses()
    data["describe_load_balancers"]["LoadBalancers"][0]["Scheme"] = "internet-facing"
    data.update(replaced)
    return data


def evaluate(data=None, **options):
    index = GraphIndex(build(data or public(), **options))
    [balancer] = load_balancer_entries(index)
    return index, evaluate_load_balancer_entry(index, balancer, 3)


def decisions(evaluation):
    return {item.element: item.decision for item in evaluation.elements}


def blockers(evaluation, element):
    return next(item.blocker_codes for item in evaluation.elements if item.element == element)


def ip_targets():
    data = public()
    data["describe_target_groups"]["TargetGroups"][0]["TargetType"] = "ip"
    data["describe_target_health"]["TargetHealthDescriptions"][0]["Target"]["Id"] = "10.0.2.9"
    return data


def redirecting():
    data = public(describe_rules=NO_RULES)
    data["describe_listeners"]["Listeners"][0]["DefaultActions"] = [REDIRECT]
    return data


def without_the_internet_rule():
    items = ec2_items()
    items[Op.DESCRIBE_SECURITY_GROUP_RULES] = items[Op.DESCRIBE_SECURITY_GROUP_RULES][1:]
    return items


# ---- public load balancer with a private backend ------------------------------------


def test_a_public_load_balancer_exposes_its_listener_and_reaches_a_private_backend():
    _, [found] = evaluate()
    assert found.kind == ExposureEntryKind.LOAD_BALANCER
    assert (found.protocol, found.start, found.end, found.decision) == (TCP, 80, 80, D.ALLOW)
    assert [item.element for item in found.elements] == FULL_PATH
    assert {item.decision for item in found.elements} == {D.ALLOW}
    assert [t.identity.physical_id for t in found.targets] == ["i-web"]
    assert found.entry.identity.physical_id.endswith("loadbalancer/app/web/1234567890abcdef")
    # The recorded unhealthy target is an observation only; it never changes the decision.
    assert found.observed_target_health == (TargetHealthState.UNHEALTHY,)


def test_the_evaluation_cites_the_listener_the_targets_and_the_backend_membership():
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
    [backend] = found.targets
    edges = index.outgoing_edges(backend.id, attachment_kind="security_group_membership")
    assert edges and set(edges[0].evidence_ids) <= set(found.evidence_ids)
    assert list(found.evidence_ids) == sorted(found.evidence_ids)


# ---- decisions along the path -------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "element", "expected"),
    [
        ({"data": responses()}, X.SCHEME, (D.DENY, ["denied"])),
        (
            {"data": public(describe_listeners={"Listeners": []}, describe_rules=NO_RULES)},
            X.LISTENER,
            (D.DENY, ["denied"]),
        ),
        ({"ec2": without_the_internet_rule()}, X.SECURITY_GROUP, (D.DENY, ["denied"])),
        ({"ec2": ec2_items(backend_rules=[])}, X.TARGET, (D.DENY, ["denied"])),
        (
            {
                "data": public(
                    describe_target_health={"TargetHealthDescriptions": []},
                    describe_rules=NO_RULES,
                )
            },
            X.TARGET,
            (D.DENY, ["denied"]),
        ),
        ({"ec2": ec2_items(acl=False)}, X.NACL, (D.UNKNOWN, ["missing_evidence"])),
        ({"data": ip_targets()}, X.TARGET, (D.UNKNOWN, ["missing_evidence"])),
        ({"data": redirecting()}, X.TARGET, (D.UNKNOWN, ["unsupported_element"])),
    ],
    ids=[
        "internal-scheme",
        "no-listeners",
        "group-closed-to-the-internet",
        "backend-closed-to-the-balancer",
        "no-registered-targets",
        "acls-unrecorded",
        "ip-targets",
        "redirect-action",
    ],
)
def test_each_variant_decides_its_element_and_the_composed_path(variant, element, expected):
    _, [found] = evaluate(**variant)
    item = next(item for item in found.elements if item.element == element)
    assert (item.decision, item.blocker_codes) == expected
    assert found.decision == expected[0]


def test_a_missing_default_route_denies_and_missing_route_records_are_unknown():
    _, [found] = evaluate(ec2=ec2_items(public_route=False))
    assert decisions(found)[X.ROUTE] == D.DENY
    items = ec2_items()
    del items[Op.DESCRIBE_ROUTE_TABLES]
    _, [found] = evaluate(ec2=items)
    assert decisions(found)[X.ROUTE] == D.UNKNOWN
    assert blockers(found, X.ROUTE) == ["missing_evidence"]
    assert found.decision == D.UNKNOWN


def test_network_balancers_without_security_groups_omit_that_element():
    data = public(describe_rules=NO_RULES)
    data["describe_load_balancers"]["LoadBalancers"][0].update(
        Type="network", LoadBalancerArn=NLB, SecurityGroups=[]
    )
    data["describe_listeners"]["Listeners"][0].update(
        LoadBalancerArn=NLB, Protocol="TCP", ListenerArn=LISTENER.replace("app/web", "net/tcp")
    )
    open_backend = ec2_items(backend_rules=[sg_rule("sg-backend", 8080)])  # no group to reference
    _, [found] = evaluate(data, ec2=open_backend)
    assert X.SECURITY_GROUP not in decisions(found)
    assert found.decision == D.ALLOW
