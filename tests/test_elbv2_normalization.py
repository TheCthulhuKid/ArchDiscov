from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest
from botocore.exceptions import ClientError

from archdiscov.ingestion.elbv2.collector import collect_inventory
from archdiscov.ingestion.elbv2.normalization import Elbv2Normalized, normalize_inventory
from archdiscov.ingestion.operations import Elbv2Operation as Op
from archdiscov.models.architecture import UnknownResource
from archdiscov.models.enums import AwsRegion, CoverageStatus, LoadBalancerType, TargetType
from archdiscov.models.load_balancing import Listener, ListenerRule, LoadBalancer, TargetGroup

from .aws_fakes import ACCOUNT
from .elbv2_data import (
    GROUP,
    LB,
    LISTENER,
    RULE,
    Elbv2Client,
    Elbv2Session,
    config,
    context,
    responses,
)


def inventory(data=None):
    session = Elbv2Session(Elbv2Client(data))
    return collect_inventory(session, config(), context(session), AwsRegion.US_EAST_1)


def normalize(data=None):
    return normalize_inventory(UUID(int=15), ACCOUNT, AwsRegion.US_EAST_1, inventory(data))


def resource(result, cls):
    return next(item for item in result.resources if isinstance(item, cls))


def citation(result, item, pointer):
    return [result.evidence[key] for key in item.fact_evidence[pointer]]


def test_flat_arn_resources_with_precise_facts():
    result = normalize()
    assert isinstance(result, Elbv2Normalized)
    assert {item.identity.physical_id for item in result.resources} == {LB, LISTENER, RULE, GROUP}
    assert all(not item.children for item in result.resources)
    for item in result.resources:
        document = item.model_dump()
        assert type(item).model_validate(document) == item
        for pointer, evidence_ids in item.fact_evidence.items():
            value = document
            for token in pointer.lstrip("/").split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[int(token)] if isinstance(value, list) else value[token]
            assert all(result.evidence[key].resource_id == item.id for key in evidence_ids)
    lb = resource(result, LoadBalancer)
    assert lb.lb_type is LoadBalancerType.APPLICATION
    assert lb.scheme == "internal"
    assert lb.subnet_ids == ["subnet-a"]
    assert lb.security_group_ids == ["sg-a"]
    assert lb.attributes["availability_zones"] == [
        {"ZoneName": "us-east-1a", "SubnetId": "subnet-a"}
    ]
    assert lb.attributes["state"] == {"Code": "active"}
    assert citation(result, lb, "/subnet_ids/0")[0].configuration_path.endswith(
        ".AvailabilityZones[0].SubnetId"
    )
    assert citation(result, lb, "/security_group_ids/0")[0].configuration_path.endswith(
        ".SecurityGroups[0]"
    )
    listener = resource(result, Listener)
    assert listener.load_balancer_arn == LB
    assert listener.actions[0].forward_groups[0].arn == GROUP
    assert citation(result, listener, "/actions/0/forward_groups/0/arn")[
        0
    ].configuration_path.endswith(".DefaultActions[0].ForwardConfig.TargetGroups[0].TargetGroupArn")
    rule = resource(result, ListenerRule)
    assert rule.listener_arn == LISTENER
    assert citation(result, rule, "/priority")[0].configuration_path.endswith(".Rules[0].Priority")
    assert citation(result, rule, "/listener_arn")[0].configuration_path == (
        f'Request[{{"ListenerArn":"{LISTENER}"}}].ListenerArn'
    )
    assert citation(result, rule, "/conditions/0")[0].configuration_path.endswith(".Conditions[0]")
    assert citation(result, rule, "/actions/0/target_group_arn")[0].value == GROUP
    group = resource(result, TargetGroup)
    assert group.target_type is TargetType.INSTANCE
    assert group.targets[0].id == "i-web"
    assert group.targets[0].health_state == "unhealthy"
    assert citation(result, group, "/targets/0/id")[0].configuration_path.endswith(
        ".TargetHealthDescriptions[0].Target.Id"
    )
    assert group.attributes["tags"] == {"team": "infra"}
    assert ".TagDescriptions[" in citation(result, group, "/attributes/tags")[0].configuration_path


def test_request_parent_and_list_order_are_not_action_order():
    data = responses()
    listener = data["describe_listeners"]["Listeners"][0]
    del listener["LoadBalancerArn"]
    listener["DefaultActions"] = [
        {"Type": "forward", "Order": 20, "TargetGroupArn": GROUP},
        {
            "Type": "forward",
            "Order": 1,
            "ForwardConfig": {
                "TargetGroups": [
                    {"TargetGroupArn": GROUP, "Weight": 5},
                    {"TargetGroupArn": GROUP + "2", "Weight": 95},
                ]
            },
        },
    ]
    result = normalize(data)
    item = resource(result, Listener)
    assert item.load_balancer_arn == LB
    assert citation(result, item, "/load_balancer_arn")[0].configuration_path == (
        f'Request[{{"LoadBalancerArn":"{LB}"}}].LoadBalancerArn'
    )
    assert [action.order for action in item.actions] == [20, 1]
    assert [group.weight for group in item.actions[1].forward_groups] == [5, 95]
    assert citation(result, item, "/actions/1/forward_groups/1/weight")[0].value == 95


def test_duplicate_resources_retain_both_item_and_fact_citations():
    collected = inventory()
    op = Op.DESCRIBE_LISTENERS
    entry = collected.results[op].responses[0]
    collected.results[op] = replace(
        collected.results[op],
        responses=[entry, replace(entry, path=entry.path.replace("Page[0]", "Page[1]"))],
    )
    before = deepcopy(collected)
    result = normalize_inventory(UUID(int=15), ACCOUNT, AwsRegion.US_EAST_1, collected)
    assert collected == before
    item = resource(result, Listener)
    assert len([r for r in result.resources if r.identity.physical_id == LISTENER]) == 1
    assert len(item.evidence_ids) == 2
    assert len(citation(result, item, "/actions/0/forward_groups/0/arn")) == 2


def test_protected_summaries_and_evidence_keep_redaction_paths():
    data = responses()
    data["describe_listeners"]["Listeners"][0]["DefaultActions"] = [
        {
            "Type": "authenticate-oidc",
            "AuthenticateOidcConfig": {
                "Issuer": "https://issuer.example",
                "ClientSecret": "secret-oidc",
                "AuthenticationRequestExtraParams": {"q": "secret-extra"},
            },
        },
        {
            "Type": "fixed-response",
            "FixedResponseConfig": {
                "StatusCode": "403",
                "MessageBody": "secret-body",
            },
        },
    ]
    data["describe_rules"]["Rules"][0]["Conditions"] = [
        {
            "Field": "http-header",
            "HttpHeaderConfig": {
                "HttpHeaderName": "Authorization",
                "Values": ["secret-header"],
            },
        },
        {
            "Field": "query-string",
            "QueryStringConfig": {
                "Values": [{"Key": "q", "Value": "secret-query"}],
            },
        },
    ]
    result = normalize(data)
    assert "secret-" not in str(result)
    item = resource(result, Listener)
    assert item.actions[0].configuration["AuthenticateOidcConfig"]["Issuer"] == (
        "https://issuer.example"
    )
    whole = result.evidence[item.evidence_ids[0]]
    assert "/DefaultActions/0/AuthenticateOidcConfig/ClientSecret" in whole.redacted_paths
    action = citation(result, item, "/actions/0")[0]
    assert "/AuthenticateOidcConfig/AuthenticationRequestExtraParams/q" in action.redacted_paths
    condition = citation(result, resource(result, ListenerRule), "/conditions/0")[0]
    assert "/HttpHeaderConfig/Values" in condition.redacted_paths


def test_unknown_values_and_malformed_nested_fields_keep_surrounding_resources():
    data = responses()
    data["describe_load_balancers"]["LoadBalancers"][0]["Scheme"] = "future-scheme"
    data["describe_listeners"]["Listeners"][0]["Port"] = "not-a-port"
    data["describe_listeners"]["Listeners"][0]["DefaultActions"] = [
        {"Type": "future-action", "Config": {"ClientSecret": "secret-unknown"}},
        {
            "Type": "forward",
            "ForwardConfig": {
                "TargetGroups": [
                    {"TargetGroupArn": GROUP, "Weight": -1},
                ]
            },
        },
    ]
    data["describe_target_health"]["TargetHealthDescriptions"][0]["TargetHealth"]["State"] = (
        "future"
    )
    result = normalize(data)
    assert len(result.resources) == 4
    assert resource(result, LoadBalancer).scheme is None
    listener = resource(result, Listener)
    assert listener.port is None
    assert len(listener.actions) == 1
    assert listener.actions[0].forward_groups[0].weight is None
    assert citation(result, listener, "/actions/0")[0].configuration_path.endswith(
        "DefaultActions[1]"
    )
    assert resource(result, TargetGroup).targets[0].health_state is None
    assert "secret-unknown" not in str(result)
    paths = [value.configuration_path for c in result.coverage for value in c.unresolved_values]
    assert any(path.endswith(".Scheme") for path in paths)
    assert any(path.endswith(".DefaultActions[0].Type") for path in paths)
    assert any(path.endswith(".ForwardConfig.TargetGroups[0].Weight") for path in paths)
    assert any(path.endswith(".TargetHealth.State") for path in paths)
    assert all(
        c.status == CoverageStatus.INCOMPLETE for c in result.coverage if c.unresolved_values
    )


def test_unsupported_lb_is_only_an_arn_summary():
    data = responses()
    data["describe_load_balancers"]["LoadBalancers"] = [
        {
            "LoadBalancerArn": LB.replace("/app/", "/gwy/"),
            "Type": "gateway",
        }
    ]
    data["describe_listeners"] = {"Listeners": []}
    data["describe_rules"] = {"Rules": []}
    result = normalize(data)
    item = resource(result, UnknownResource)
    assert item.identity.physical_id == LB.replace("/app/", "/gwy/")
    assert item.aws_resource_type == "AWS::ElasticLoadBalancingV2::LoadBalancer"
    assert item.attributes["summary"]["Type"] == "gateway"
    assert not any(isinstance(r, LoadBalancer) for r in result.resources)


@pytest.mark.parametrize("failed", [False, True])
def test_empty_health_is_known_only_on_success(failed):
    data = responses()
    data["describe_target_health"] = (
        ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeTargetHealth"
        )
        if failed
        else {"TargetHealthDescriptions": []}
    )
    result = normalize(data)
    group = resource(result, TargetGroup)
    assert group.targets == (None if failed else [])
    if failed:
        assert group.attributes["limitations"]
        assert next(
            c for c in result.coverage if c.operation == Op.DESCRIBE_TARGET_HEALTH
        ).status == (CoverageStatus.FAILED)


def test_missing_details_and_tag_replies_are_not_empty_configurations():
    data = responses()
    del data["describe_target_groups"]["TargetGroups"][0]["HealthCheckProtocol"]
    del data["describe_target_groups"]["TargetGroups"][0]["HealthCheckPort"]
    del data["describe_rules"]["Rules"][0]["Conditions"]
    data["describe_tags"] = {"TagDescriptions": []}
    result = normalize(data)
    assert resource(result, TargetGroup).health_check is None
    assert resource(result, ListenerRule).conditions is None
    assert all("tags" not in r.attributes and r.attributes["limitations"] for r in result.resources)
    paths = [v.configuration_path for c in result.coverage for v in c.unresolved_values]
    assert any(path.endswith(".Conditions") for path in paths)
    assert any(path.endswith(".HealthCheckProtocol") for path in paths)


def test_full_addressing_health_checks_and_target_field_paths():
    data = responses()
    lb = data["describe_load_balancers"]["LoadBalancers"][0]
    lb["AvailabilityZones"][0].update(
        {
            "LoadBalancerAddresses": [
                {
                    "IpAddress": "192.0.2.1",
                    "AllocationId": "eipalloc-a",
                    "PrivateIPv4Address": "10.0.0.1",
                    "IPv6Address": "2001:db8::1",
                }
            ],
            "SourceNatIpv6Prefixes": ["2001:db8::/80"],
            "OutpostId": "op-a",
        }
    )
    group = data["describe_target_groups"]["TargetGroups"][0]
    group.update(
        {
            "HealthCheckEnabled": True,
            "HealthCheckPath": "/ready",
            "HealthCheckIntervalSeconds": 30,
            "HealthCheckTimeoutSeconds": 5,
            "HealthyThresholdCount": 3,
            "UnhealthyThresholdCount": 2,
            "Matcher": {"HttpCode": "200-299"},
        }
    )
    data["describe_target_health"]["TargetHealthDescriptions"][0]["TargetHealth"].update(
        {
            "Description": "Health checks failed",
        }
    )
    result = normalize(data)
    assert (
        resource(result, LoadBalancer).attributes["availability_zones"] == lb["AvailabilityZones"]
    )
    normalized = resource(result, TargetGroup)
    assert normalized.health_check["HealthyThresholdCount"] == 3
    assert citation(result, normalized, "/health_check/Matcher")[0].value == {"HttpCode": "200-299"}
    for field, suffix in (
        ("port", "Target.Port"),
        ("availability_zone", "Target.AvailabilityZone"),
        ("health_state", "TargetHealth.State"),
        ("health_reason", "TargetHealth.Reason"),
        ("health_description", "TargetHealth.Description"),
    ):
        assert citation(result, normalized, f"/targets/0/{field}")[0].configuration_path.endswith(
            suffix
        )


def test_partial_health_batch_does_not_invent_an_empty_group():
    data = responses()
    other = GROUP + "2"
    data["describe_target_groups"]["TargetGroups"].append(
        {
            **data["describe_target_groups"]["TargetGroups"][0],
            "TargetGroupArn": other,
        }
    )

    def health(params):
        if params["TargetGroupArn"] == other:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeTargetHealth"
            )
        return {"TargetHealthDescriptions": []}

    data["describe_target_health"] = health
    result = normalize(data)
    groups = [r for r in result.resources if isinstance(r, TargetGroup)]
    assert len(groups) == 2
    assert all(g.targets is None and g.attributes["limitations"] for g in groups)
    health_coverage = next(c for c in result.coverage if c.operation == Op.DESCRIBE_TARGET_HEALTH)
    assert health_coverage.status == CoverageStatus.INCOMPLETE
    assert {v.resource_id for v in health_coverage.unresolved_values} == {g.id for g in groups}


def test_partial_health_batch_retains_successful_sibling_registrations():
    data = responses()
    original = deepcopy(data["describe_target_health"])
    other = GROUP + "2"
    data["describe_target_groups"]["TargetGroups"].append(
        {
            **data["describe_target_groups"]["TargetGroups"][0],
            "TargetGroupArn": other,
        }
    )

    def health(params):
        if params["TargetGroupArn"] == other:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeTargetHealth"
            )
        return original

    data["describe_target_health"] = health
    result = normalize(data)
    groups = {r.arn: r for r in result.resources if isinstance(r, TargetGroup)}
    assert groups[GROUP].targets[0].id == "i-web"
    assert groups[other].targets is None
    assert (
        f'"TargetGroupArn":"{GROUP}"'
        in citation(result, groups[GROUP], "/targets/0/id")[0].configuration_path
    )


def test_malformed_collections_and_health_entries_are_safe_and_unavailable():
    data = responses()
    data["describe_load_balancers"]["LoadBalancers"][0]["AvailabilityZones"] = "not-a-list"
    data["describe_load_balancers"]["LoadBalancers"][0]["SecurityGroups"] = None
    data["describe_listeners"]["Listeners"][0]["DefaultActions"] = None
    data["describe_target_health"]["TargetHealthDescriptions"] = [
        {
            "Target": {"Id": None, "ClientSecret": "secret-target"},
        }
    ]
    result = normalize(data)
    lb = resource(result, LoadBalancer)
    assert lb.subnet_ids is None and lb.security_group_ids is None
    assert lb.attributes["summary"]["AvailabilityZones"] == "not-a-list"
    assert resource(result, Listener).actions is None
    group = resource(result, TargetGroup)
    assert group.targets is None
    assert group.attributes["target_health"][0]["Target"]["Id"] is None
    assert "secret-target" not in str(result)
    paths = [v.configuration_path for c in result.coverage for v in c.unresolved_values]
    assert any(path.endswith(".TargetHealthDescriptions[0].Target.Id") for path in paths)


def test_tag_secret_redaction_and_exact_empty_tag_reply():
    data = responses()
    data["describe_tags"] = lambda params: {
        "TagDescriptions": [
            {
                "ResourceArn": arn,
                "Tags": [{"Key": "password", "Value": "secret-tag"}] if arn == LB else [],
            }
            for arn in params["ResourceArns"]
        ]
    }
    result = normalize(data)
    lb = resource(result, LoadBalancer)
    assert lb.attributes["tags"] == {"password": "[REDACTED]"}
    evidence = citation(result, lb, "/attributes/tags")[0]
    assert evidence.redacted_paths == ["/0/Value"]
    assert "secret-tag" not in str(result)
    assert resource(result, TargetGroup).attributes["tags"] == {}


def test_nlb_and_lambda_do_not_invent_inapplicable_fields():
    data = responses()
    nlb_arn = LB.replace("/app/", "/net/")
    lb = data["describe_load_balancers"]["LoadBalancers"][0]
    lb.update({"LoadBalancerArn": nlb_arn, "Type": "network"})
    del lb["SecurityGroups"]
    listener = data["describe_listeners"]["Listeners"][0]
    listener.update(
        {
            "ListenerArn": LISTENER.replace("/app/", "/net/"),
            "LoadBalancerArn": nlb_arn,
            "Protocol": "TCP",
        }
    )
    data["describe_target_groups"]["TargetGroups"] = [
        {
            "TargetGroupArn": GROUP,
            "TargetType": "lambda",
            "HealthCheckEnabled": False,
        }
    ]
    data["describe_target_health"] = {"TargetHealthDescriptions": []}
    result = normalize(data)
    assert resource(result, LoadBalancer).security_group_ids is None
    assert not any(isinstance(r, ListenerRule) for r in result.resources)
    group = resource(result, TargetGroup)
    assert group.protocol is None and group.port is None and group.vpc_id is None
    assert group.health_check == {"HealthCheckEnabled": False}
    assert group.targets == []
    assert not any(c.unresolved_values for c in result.coverage)


def test_malformed_nested_entries_preserve_original_indices_and_safe_summaries():
    data = responses()
    listener = data["describe_listeners"]["Listeners"][0]
    listener["Protocol"] = "future-protocol"
    listener["DefaultActions"][0]["ForwardConfig"]["TargetGroups"] = [
        None,
        {"TargetGroupArn": ""},
        {"TargetGroupArn": GROUP, "Weight": 5},
    ]
    rule = data["describe_rules"]["Rules"][0]
    rule["Conditions"] = [None, {"Field": "future-condition"}, *rule["Conditions"]]
    result = normalize(data)
    normalized = resource(result, Listener)
    assert normalized.protocol is None
    assert len(normalized.actions[0].forward_groups) == 1
    assert citation(result, normalized, "/actions/0/forward_groups/0/arn")[
        0
    ].configuration_path.endswith(".ForwardConfig.TargetGroups[2].TargetGroupArn")
    assert citation(result, resource(result, ListenerRule), "/conditions/0")[
        0
    ].configuration_path.endswith(".Conditions[2]")
    assert resource(result, ListenerRule).attributes["summary"]["Conditions"] == rule["Conditions"]
    paths = [v.configuration_path for c in result.coverage for v in c.unresolved_values]
    assert any(path.endswith(".ForwardConfig.TargetGroups[0]") for path in paths)
    assert any(path.endswith(".ForwardConfig.TargetGroups[1].TargetGroupArn") for path in paths)
    assert any(path.endswith(".Conditions[1].Field") for path in paths)
