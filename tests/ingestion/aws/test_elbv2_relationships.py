import pytest

from archdiscov.ingestion.elbv2.relationships import (
    merge_resources,
    resolve_relationships,
    walk_resources,
)
from archdiscov.models.architecture import (
    Ec2Instance,
    Subnet,
)
from archdiscov.models.enums import (
    AwsRegion,
    DependencyRelationship,
    LoadBalancerType,
    TargetType,
)
from archdiscov.models.resources.load_balancing import (
    Listener,
    ListenerRule,
    LoadBalancer,
    TargetGroup,
)
from tests.support.elbv2_data import ACCOUNT, LB, responses
from tests.support.elbv2_snapshot import ec2_roots, identity, normalized, snapshot


def test_merge_preserves_ec2_tree_and_exact_source_owned_links():
    result = normalized()
    roots = ec2_roots()
    original = roots[0]
    assert merge_resources(roots, result.resources) is roots
    assert roots[0] is original
    snap = snapshot(result, roots)
    resources = list(walk_resources(roots))
    assert len(resources) == len({r.id for r in resources}) == 8
    load_balancer = next(r for r in resources if isinstance(r, LoadBalancer))
    listener = next(r for r in load_balancer.children if isinstance(r, Listener))
    assert isinstance(listener.children[0], ListenerRule)
    links = snap.dependencies_by_region[AwsRegion.US_EAST_1]
    assert len(links) == 7
    assert all(link.target.kind == "local" for link in links)
    weighted = next(
        link
        for link in links
        if link.source_id == listener.id and link.relationship == "forwards_to"
    )
    assert weighted.attributes["weight"] == 100
    assert weighted.attributes["action_order"] == 1
    assert snap.evidence[weighted.evidence_ids[0]].configuration_path.endswith(
        "DefaultActions[0].ForwardConfig.TargetGroups[0].TargetGroupArn"
    )
    target_group = next(r for r in resources if isinstance(r, TargetGroup))
    health = next(link for link in links if link.source_id == target_group.id)
    assert health.attributes["health_state"] == "unhealthy"
    assert health.attributes["port"] == 8080
    assert health.attributes["availability_zone"] == "us-east-1a"
    assert all(
        snap.evidence[e].resource_id == link.source_id for link in links for e in link.evidence_ids
    )


def test_elb_only_missing_parents_targets_are_explicit():
    result = normalized()
    roots = merge_resources([], result.resources)
    snap = snapshot(result, roots)
    assert len(roots) == 2
    assert all("unresolved_parent" in r.attributes for r in roots)
    group = next(r for r in roots if isinstance(r, TargetGroup))
    registration = next(
        link for link in snap.dependencies_by_region["us-east-1"] if link.source_id == group.id
    )
    assert registration.target.kind == "unresolved"
    assert registration.attributes["observed_identifier"] == "i-web"
    assert registration.target.source_path.endswith("Target.Id")


def balancers(result, *kinds):
    load_balancer = next(r for r in result.resources if isinstance(r, LoadBalancer))
    return [load_balancer.model_copy(update={"lb_type": kind}) for kind in kinds]


def instance(identifier):
    return Ec2Instance(identity=identity(identifier))


def subnet(identifier):
    return Subnet(identity=identity(identifier))


LAMBDA = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:worker"
APPLICATION, NETWORK = LoadBalancerType.APPLICATION, LoadBalancerType.NETWORK


@pytest.mark.parametrize(
    "target_type,identifier,candidates,kind",
    [
        (TargetType.INSTANCE, "i-web", lambda r: [instance("i-web"), subnet("i-web")], "local"),
        (TargetType.INSTANCE, "i-web", lambda r: [subnet("i-web")], "unresolved"),
        (TargetType.ALB, LB, lambda r: balancers(r, APPLICATION, NETWORK), "local"),
        (TargetType.ALB, LB, lambda r: balancers(r, NETWORK), "unresolved"),
        (TargetType.LAMBDA, LAMBDA, lambda r: [instance(LAMBDA)], "external"),
        (TargetType.IP, "10.0.0.7", lambda r: [instance("10.0.0.7")], "unresolved"),
    ],
    ids=["instance", "instance-wrong-type", "alb", "alb-only-nlb", "lambda", "ip"],
)
def test_registration_resolves_only_a_unique_resource_of_the_expected_type(
    target_type, identifier, candidates, kind
):
    result = normalized()
    group = next(r for r in result.resources if isinstance(r, TargetGroup))
    group.target_type = target_type
    group.targets[0].id = identifier
    links = resolve_relationships([group, *candidates(result)], result.evidence)
    (link,) = (link for link in links if link.source_id == group.id)
    assert link.target.kind == kind
    assert link.evidence_ids == group.fact_evidence["/targets/0/id"]
    assert link.attributes["observed_identifier"] == identifier


def test_nonforward_actions_never_become_forwarding_edges():
    action_type = "authenticate-oidc"
    data = responses()
    data["describe_listeners"]["Listeners"][0]["DefaultActions"] = [
        {
            "Type": action_type,
            "AuthenticateOidcConfig": {
                "Issuer": "https://issuer",
                "ClientSecret": "protected-secret",
            },
        }
    ]
    result = normalized(data)
    roots = merge_resources([], result.resources)
    snap = snapshot(result, roots)
    listener = next(r for r in walk_resources(roots) if isinstance(r, Listener))
    assert listener.actions[0].type == action_type
    assert not any(
        link.source_id == listener.id and link.relationship == DependencyRelationship.FORWARDS_TO
        for link in snap.dependencies_by_region["us-east-1"]
    )
    assert "protected-" not in snap.model_dump_json()
