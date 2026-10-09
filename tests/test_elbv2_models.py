import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.enums import (
    AttachmentKind,
    ListenerActionType,
    LoadBalancerProtocol,
    LoadBalancerScheme,
    LoadBalancerType,
    ResourceType,
    TargetHealthState,
    TargetType,
)
from archdiscov.models.load_balancing import Listener, ListenerRule, LoadBalancer, TargetGroup
from archdiscov.models.load_balancing_sanitization import sanitize_configuration
from archdiscov.models.load_balancing_values import (
    ListenerAction,
    RegisteredTarget,
    RuleCondition,
    WeightedTargetGroup,
)
from archdiscov.services import load_snapshot, save_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"
ARN_PREFIX = "arn:aws:elasticloadbalancing:eu-west-1:123456789012:"
LB_ARN = ARN_PREFIX + "loadbalancer/app/web/123"
LISTENER_ARN = ARN_PREFIX + "listener/app/web/123/456"
RULE_ARN = ARN_PREFIX + "listener-rule/app/web/123/456/789"
TG_ARN = ARN_PREFIX + "targetgroup/web/123"


def identity(arn):
    return {
        "account_id": "123456789012",
        "region": "eu-west-1",
        "service": "elasticloadbalancing",
        "physical_id": arn,
    }


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


def test_load_balancing_fixture_round_trips_with_typed_children(tmp_path):
    snapshot = load_snapshot(FIXTURES / "load-balancing.json")
    resources = {r.arn: r for r in flatten(snapshot.regions["eu-west-1"].resources)}
    assert isinstance(resources[LB_ARN], LoadBalancer)
    assert isinstance(resources[LISTENER_ARN], Listener)
    assert isinstance(resources[RULE_ARN], ListenerRule)
    assert isinstance(resources[TG_ARN], TargetGroup)
    assert resources[LB_ARN].lb_type is LoadBalancerType.APPLICATION
    assert resources[LB_ARN].scheme is LoadBalancerScheme.INTERNET_FACING
    assert resources[LISTENER_ARN].protocol is LoadBalancerProtocol.HTTPS
    assert resources[LISTENER_ARN].actions[0].type is ListenerActionType.FORWARD
    assert resources[LISTENER_ARN].actions[0].forward_groups[0].arn == TG_ARN
    assert resources[TG_ARN].target_type is TargetType.INSTANCE
    assert resources[TG_ARN].targets[0].health_state is TargetHealthState.HEALTHY
    assert resources[RULE_ARN].priority == "10"
    assert resources[LB_ARN].id == resources[LB_ARN].identity.stable_id
    saved = save_snapshot(snapshot, tmp_path / "out.json")
    assert load_snapshot(saved) == snapshot


@pytest.mark.parametrize(
    ("arn", "fact_path", "source_path", "value"),
    [
        (LISTENER_ARN, "/actions/0", "Listeners[0].DefaultActions[0]", "forward"),
        (RULE_ARN, "/conditions/0", "Rules[0].Conditions[0]", "path-pattern"),
        (TG_ARN, "/targets/0", "TargetHealthDescriptions[0]", "i-web"),
    ],
)
def test_fixture_citations_resolve_to_exact_entries(arn, fact_path, source_path, value):
    snapshot = load_snapshot(FIXTURES / "load-balancing.json")
    resources = {r.arn: r for r in flatten(snapshot.regions["eu-west-1"].resources)}
    resource = resources[arn]
    cited = snapshot.evidence[resource.fact_evidence[fact_path][0]]
    assert cited.resource_id == resource.id
    assert cited.configuration_path == source_path
    assert value in json.dumps(cited.value)


@pytest.mark.parametrize(
    ("model", "arn", "fields"),
    [
        (LoadBalancer, LB_ARN, ["subnet_ids", "security_group_ids"]),
        (Listener, LISTENER_ARN, ["actions"]),
        (ListenerRule, RULE_ARN, ["conditions", "actions"]),
        (TargetGroup, TG_ARN, ["targets"]),
    ],
)
def test_unavailable_collections_are_distinct_from_empty(model, arn, fields):
    unavailable = model(identity=identity(arn), arn=arn)
    empty = model(identity=identity(arn), arn=arn, **{field: [] for field in fields})
    for field in fields:
        assert getattr(unavailable, field) is None
        assert getattr(empty, field) == []


@pytest.mark.parametrize("model,arn", [(Listener, LISTENER_ARN), (TargetGroup, TG_ARN)])
@pytest.mark.parametrize("port", [0, 65536])
def test_resource_port_bounds(model, arn, port):
    with pytest.raises(ValidationError):
        model(identity=identity(arn), arn=arn, port=port)


@pytest.mark.parametrize("port", [0, 65536])
def test_registered_target_port_bounds(port):
    with pytest.raises(ValidationError):
        RegisteredTarget(id="i-web", port=port)


@pytest.mark.parametrize("weight", [-1, 1000])
def test_forward_weight_bounds(weight):
    with pytest.raises(ValidationError):
        WeightedTargetGroup(arn=TG_ARN, weight=weight)


@pytest.mark.parametrize("port", [1, 65535])
@pytest.mark.parametrize("weight", [0, 999])
def test_port_and_weight_boundaries_are_accepted(port, weight):
    assert Listener(identity=identity(LISTENER_ARN), arn=LISTENER_ARN, port=port).port == port
    assert TargetGroup(identity=identity(TG_ARN), arn=TG_ARN, port=port).port == port
    assert RegisteredTarget(id="i-web", port=port).port == port
    assert WeightedTargetGroup(arn=TG_ARN, weight=weight).weight == weight


@pytest.mark.parametrize(
    "model,arn",
    [
        (LoadBalancer, LB_ARN),
        (Listener, LISTENER_ARN),
        (ListenerRule, RULE_ARN),
        (TargetGroup, TG_ARN),
    ],
)
@pytest.mark.parametrize("incorrect", [{"physical_id": "not-the-arn"}, {"service": "ec2"}])
def test_resources_require_arn_identity(model, arn, incorrect):
    with pytest.raises(ValidationError, match="ELBv2 identity"):
        model(identity={**identity(arn), **incorrect}, arn=arn)


@pytest.mark.parametrize(
    "action_type,configuration",
    [
        ("authenticate-oidc", {"ClientSecret": "oidc-secret"}),
        ("authenticate-oidc", {"AuthenticationRequestExtraParams": {"login": "login-secret"}}),
        ("authenticate-cognito", {"AuthenticationRequestExtraParams": {"hint": "hint-secret"}}),
        ("fixed-response", {"MessageBody": "body-secret", "StatusCode": "403"}),
        ("redirect", {"Host": "example.com", "metadata": {"password": "password-secret"}}),
    ],
)
def test_actions_independently_sanitize_configuration(action_type, configuration):
    original = copy.deepcopy(configuration)
    action = ListenerAction(type=action_type, configuration=configuration)
    assert configuration == original
    assert "secret" not in action.model_dump_json()
    assert "[REDACTED]" in action.model_dump_json()
    assert ListenerAction.model_validate_json(action.model_dump_json()) == action
    if action_type == "fixed-response":
        assert action.configuration["StatusCode"] == "403"
    if action_type == "redirect":
        assert action.configuration["Host"] == "example.com"


@pytest.mark.parametrize(
    "field,configuration",
    [
        ("http-header", {"HttpHeaderName": "X-Auth", "Values": ["header-secret"]}),
        ("query-string", {"Values": [{"Key": "user", "Value": "query-secret"}]}),
        ("http-header", {"HttpHeaderConfig": {"Values": ["nested-secret"]}}),
        ("query-string", {"QueryStringConfig": {"Values": [{"Value": "nested-secret"}]}}),
    ],
)
def test_conditions_independently_sanitize_private_values(field, configuration):
    original = copy.deepcopy(configuration)
    condition = RuleCondition(field=field, configuration=configuration)
    assert configuration == original
    assert "secret" not in condition.model_dump_json()
    assert "[REDACTED]" in condition.model_dump_json()
    assert RuleCondition.model_validate_json(condition.model_dump_json()) == condition


def test_shared_sanitizer_paths_are_escaped_and_idempotent():
    configuration = {
        "AuthenticateOidcConfig": {
            "ClientSecret": "client-secret",
            "AuthenticationRequestExtraParams": {"a/b~c": "extra-secret"},
            "Issuer": "https://example.com",
        },
        "FixedResponseConfig": {"MessageBody": "body-secret", "StatusCode": "403"},
        "HttpHeaderConfig": {"Values": ["header-secret"], "RegexValues": ["regex-secret"]},
        "metadata": {"password": "password-secret"},
    }
    original = copy.deepcopy(configuration)
    safe, paths = sanitize_configuration(configuration)
    assert configuration == original
    assert paths == sorted(
        [
            "/AuthenticateOidcConfig/ClientSecret",
            "/AuthenticateOidcConfig/AuthenticationRequestExtraParams/a~1b~0c",
            "/FixedResponseConfig/MessageBody",
            "/HttpHeaderConfig/Values",
            "/HttpHeaderConfig/RegexValues",
            "/metadata/password",
        ]
    )
    assert safe["AuthenticateOidcConfig"]["Issuer"] == "https://example.com"
    assert safe["FixedResponseConfig"]["StatusCode"] == "403"
    assert sanitize_configuration(safe) == (safe, paths)


def test_snapshot_persistence_independently_redacts_typed_values(tmp_path):
    data = json.loads((FIXTURES / "load-balancing.json").read_text())
    listener = data["regions"]["eu-west-1"]["resources"][0]["children"][0]
    listener["actions"] = [
        {
            "type": "authenticate-oidc",
            "configuration": {
                "AuthenticateOidcConfig": {
                    "ClientSecret": "client-secret",
                    "AuthenticationRequestExtraParams": {"login": "login-secret"},
                    "Issuer": "https://example.com",
                },
            },
        }
    ]
    rule = listener["children"][0]
    rule["conditions"] = [{"field": "http-header", "configuration": {"Values": ["header-secret"]}}]
    rule["actions"] = [{"type": "fixed-response", "configuration": {"MessageBody": "body-secret"}}]
    snapshot = ArchitectureSnapshot.model_validate(data)
    saved = save_snapshot(snapshot, tmp_path / "safe.json")
    serialized = saved.read_text()
    for secret in ("client-secret", "login-secret", "header-secret", "body-secret"):
        assert secret not in serialized
    assert "https://example.com" in serialized
    assert "[REDACTED]" in serialized
    assert load_snapshot(saved) == snapshot


def test_non_private_condition_values_and_safe_health_checks_are_preserved():
    condition = RuleCondition(field="path-pattern", configuration={"Values": ["/api/*"]})
    assert condition.configuration == {"Values": ["/api/*"]}
    group = TargetGroup(
        identity=identity(TG_ARN),
        arn=TG_ARN,
        health_check={"Path": "/health", "password": "health-secret"},
    )
    assert group.health_check == {"Path": "/health", "password": "[REDACTED]"}


def test_resource_and_attachment_enum_names():
    assert ResourceType.ELBV2_LOAD_BALANCER == "AWS::ElasticLoadBalancingV2::LoadBalancer"
    assert ResourceType.ELBV2_LISTENER == "AWS::ElasticLoadBalancingV2::Listener"
    assert ResourceType.ELBV2_LISTENER_RULE == "AWS::ElasticLoadBalancingV2::ListenerRule"
    assert ResourceType.ELBV2_TARGET_GROUP == "AWS::ElasticLoadBalancingV2::TargetGroup"
    assert AttachmentKind.SUBNET_MEMBERSHIP == "subnet_membership"
    assert AttachmentKind.LISTENER_LOAD_BALANCER == "listener_load_balancer"
    assert AttachmentKind.RULE_LISTENER == "rule_listener"
