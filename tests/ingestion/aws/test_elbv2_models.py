import copy

import pytest
from pydantic import ValidationError

from archdiscov.models.enums import (
    ListenerActionType,
    LoadBalancerProtocol,
    LoadBalancerScheme,
    LoadBalancerType,
    TargetHealthState,
    TargetType,
)
from archdiscov.models.resources.load_balancing import (
    Listener,
    ListenerRule,
    LoadBalancer,
    TargetGroup,
)
from archdiscov.models.resources.load_balancing_values import ListenerAction, RuleCondition
from archdiscov.services import load_snapshot, save_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
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


def test_resources_require_arn_identity():
    with pytest.raises(ValidationError, match="ELBv2 identity"):
        Listener(
            identity={**identity(LISTENER_ARN), "physical_id": "not-the-arn"}, arn=LISTENER_ARN
        )


def test_typed_values_sanitize_private_configuration_without_mutating_it():
    configuration = {"ClientSecret": "oidc-secret", "Issuer": "https://issuer.example"}
    original = copy.deepcopy(configuration)
    action = ListenerAction(type="authenticate-oidc", configuration=configuration)
    assert configuration == original
    assert action.configuration == {
        "ClientSecret": "[REDACTED]",
        "Issuer": "https://issuer.example",
    }

    condition = RuleCondition(field="http-header", configuration={"Values": ["header-secret"]})
    assert "header-secret" not in condition.model_dump_json()
    path = RuleCondition(field="path-pattern", configuration={"Values": ["/api/*"]})
    assert path.configuration == {"Values": ["/api/*"]}

    group = TargetGroup(
        identity=identity(TG_ARN),
        arn=TG_ARN,
        health_check={"Path": "/health", "password": "health-secret"},
    )
    assert group.health_check == {"Path": "/health", "password": "[REDACTED]"}
