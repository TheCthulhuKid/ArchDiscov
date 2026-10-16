from datetime import UTC, datetime
from uuid import UUID

import pytest

from archdiscov.ingestion.elbv2.collector import collect_inventory
from archdiscov.ingestion.elbv2.normalization import normalize_inventory
from archdiscov.ingestion.elbv2.relationships import (
    merge_resources,
    resolve_relationships,
    walk_resources,
)
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    Ec2Instance,
    RegionArchitecture,
    Subnet,
    Vpc,
)
from archdiscov.models.enums import (
    AwsRegion,
    AwsService,
    DependencyRelationship,
    LoadBalancerType,
    ResourceType,
    TargetType,
)
from archdiscov.models.resources.load_balancing import (
    Listener,
    ListenerRule,
    LoadBalancer,
    TargetGroup,
)
from archdiscov.models.resources.network import SecurityGroup
from archdiscov.services import load_snapshot, save_snapshot
from tests.support.elbv2_data import (
    ACCOUNT,
    LB,
    Elbv2Client,
    Elbv2Session,
    config,
    context,
    responses,
)


def identity(identifier):
    return {
        "account_id": ACCOUNT,
        "region": "us-east-1",
        "service": "ec2",
        "physical_id": identifier,
    }


def ec2_roots():
    return [
        Vpc(
            identity=identity("vpc-a"),
            children=[
                Subnet(
                    identity=identity("subnet-a"),
                    children=[Ec2Instance(identity=identity("i-web"))],
                ),
                SecurityGroup(identity=identity("sg-a")),
            ],
        )
    ]


def normalized(data=None):
    session = Elbv2Session(Elbv2Client(data))
    inventory = collect_inventory(session, config(), context(session), AwsRegion.US_EAST_1)
    return normalize_inventory(UUID(int=15), ACCOUNT, AwsRegion.US_EAST_1, inventory)


def snapshot(result, roots):
    links = resolve_relationships(roots, result.evidence)
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=UUID(int=15),
        account_id=ACCOUNT,
        collection_started_at=datetime(2025, 1, 1, tzinfo=UTC),
        collection_completed_at=datetime(2025, 1, 2, tzinfo=UTC),
        source={"kind": "aws"},
        regions={"us-east-1": RegionArchitecture(resources=roots)},
        coverage={"regions": ["us-east-1"], "account_wide": False, "operations": result.coverage},
        dependencies_by_region={"us-east-1": links},
        evidence=result.evidence,
    )


def test_merge_preserves_ec2_tree_and_exact_source_owned_links(tmp_path):
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
    assert load_snapshot(save_snapshot(snap, tmp_path / "linked.json")) == snap


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


@pytest.mark.parametrize(
    "target_type,identifier,expected",
    [
        (TargetType.IP, "10.0.0.7", "unresolved"),
        (TargetType.LAMBDA, f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:worker", "external"),
        (TargetType.ALB, LB, "local"),
    ],
)
def test_target_kinds_do_not_infer_ip_ownership(target_type, identifier, expected):
    data = responses()
    data["describe_target_groups"]["TargetGroups"][0]["TargetType"] = target_type.value
    data["describe_target_health"]["TargetHealthDescriptions"][0]["Target"]["Id"] = identifier
    result = normalized(data)
    roots = merge_resources(ec2_roots(), result.resources)
    snap = snapshot(result, roots)
    group = next(r for r in walk_resources(roots) if isinstance(r, TargetGroup))
    link = next(
        link for link in snap.dependencies_by_region["us-east-1"] if link.source_id == group.id
    )
    assert link.target.kind == expected
    assert group.targets[0].id == identifier
    if target_type == TargetType.IP:
        assert "ownership is not inferred" in link.target.reason


@pytest.mark.parametrize("target_type", [TargetType.INSTANCE, None, TargetType.ALB])
@pytest.mark.parametrize("observation", ["missing", "wrong_type", "unique", "duplicate"])
def test_registration_requires_unique_expected_resource_type(target_type, observation):
    result = normalized()
    group = next(r for r in result.resources if isinstance(r, TargetGroup))
    group.target_type = target_type
    identifier = LB if target_type == TargetType.ALB else "i-web"
    group.targets[0].id = identifier
    expected = (
        next(r for r in result.resources if isinstance(r, LoadBalancer))
        if target_type == TargetType.ALB
        else Ec2Instance(identity=identity(identifier))
    )
    candidates = {
        "missing": [],
        "wrong_type": [Subnet(identity=identity(identifier))],
        "unique": [expected, Subnet(identity=identity(identifier))],
        "duplicate": [expected, expected.model_copy(deep=True)],
    }[observation]
    resources = [group, *candidates]
    resource_ids = [r.id for r in resources]
    citations = group.fact_evidence["/targets/0/id"]
    links = [
        link
        for link in resolve_relationships(resources, result.evidence)
        if link.source_id == group.id
    ]
    assert len(links) == 1
    link = links[0]
    assert link.evidence_ids == citations
    assert link.attributes == {
        "observed_identifier": identifier,
        "port": 8080,
        "availability_zone": "us-east-1a",
        "health_state": "unhealthy",
        "health_reason": "Target.FailedHealthChecks",
    }
    if observation == "unique":
        assert link.target.kind == "local"
        assert link.target.resource_id == expected.id
    else:
        assert link.target.kind == "unresolved"
        assert link.target.reason == (
            "target was not uniquely observed with the expected resource type"
        )
        assert link.target.source_path == result.evidence[citations[0]].configuration_path
    assert [r.id for r in resources] == resource_ids


@pytest.mark.parametrize(
    "lb_types,expected",
    [
        ([LoadBalancerType.NETWORK], "unresolved"),
        ([None], "unresolved"),
        ([LoadBalancerType.APPLICATION, LoadBalancerType.NETWORK], "local"),
    ],
)
def test_alb_registration_filters_load_balancer_type_before_uniqueness(lb_types, expected):
    result = normalized()
    group = next(r for r in result.resources if isinstance(r, TargetGroup))
    group.target_type = TargetType.ALB
    group.targets[0].id = LB
    load_balancer = next(r for r in result.resources if isinstance(r, LoadBalancer))
    candidates = [load_balancer.model_copy(update={"lb_type": kind}) for kind in lb_types]
    link = next(
        link
        for link in resolve_relationships([group, *candidates], result.evidence)
        if link.source_id == group.id
    )
    assert link.target.kind == expected


@pytest.mark.parametrize(
    "identifier,account_id",
    [
        (f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:worker", ACCOUNT),
        ("arn:aws:lambda:us-east-1:invalid:function:worker", None),
        ("arn:aws:lambda:us-east-1:123:function:worker", None),
        ("worker", None),
    ],
)
def test_lambda_registration_remains_external_without_fabricating_resources(identifier, account_id):
    result = normalized()
    group = next(r for r in result.resources if isinstance(r, TargetGroup))
    group.target_type = TargetType.LAMBDA
    group.targets[0].id = identifier
    roots = [group, Ec2Instance(identity=identity(identifier))]
    resource_ids = [r.id for r in roots]
    link = resolve_relationships(roots, result.evidence)[0]
    assert link.target.model_dump() == {
        "kind": "external",
        "identifier": identifier,
        "service": AwsService.LAMBDA,
        "region": None,
        "account_id": account_id,
        "aws_resource_type": ResourceType.LAMBDA_FUNCTION,
    }
    assert link.evidence_ids == group.fact_evidence["/targets/0/id"]
    assert [r.id for r in roots] == resource_ids


def test_ip_registration_does_not_resolve_matching_physical_identifier():
    result = normalized()
    group = next(r for r in result.resources if isinstance(r, TargetGroup))
    group.target_type = TargetType.IP
    group.targets[0].id = "10.0.0.7"
    roots = [group, Ec2Instance(identity=identity("10.0.0.7"))]
    link = resolve_relationships(roots, result.evidence)[0]
    assert link.target.kind == "unresolved"
    assert link.target.reason == "IP registration is observed; resource ownership is not inferred"
    assert link.target.source_path == (
        result.evidence[group.fact_evidence["/targets/0/id"][0]].configuration_path
    )


@pytest.mark.parametrize("target_type", list(TargetType))
def test_registration_without_citations_never_creates_an_edge(target_type):
    result = normalized()
    group = next(r for r in result.resources if isinstance(r, TargetGroup))
    group.target_type = target_type
    group.fact_evidence.clear()
    assert resolve_relationships([group], {}) == []


@pytest.mark.parametrize(
    "action_type,configuration",
    [
        (
            "redirect",
            {"RedirectConfig": {"Protocol": "HTTPS", "Port": "443", "StatusCode": "HTTP_301"}},
        ),
        (
            "fixed-response",
            {"FixedResponseConfig": {"StatusCode": "403", "MessageBody": "protected-body"}},
        ),
        (
            "authenticate-oidc",
            {
                "AuthenticateOidcConfig": {
                    "Issuer": "https://issuer",
                    "ClientSecret": "protected-secret",
                }
            },
        ),
    ],
)
def test_nonforward_actions_never_become_forwarding_edges(action_type, configuration):
    data = responses()
    data["describe_listeners"]["Listeners"][0]["DefaultActions"] = [
        {"Type": action_type, **configuration}
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
