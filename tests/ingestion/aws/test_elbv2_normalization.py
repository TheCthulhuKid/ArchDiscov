from uuid import UUID

import boto3
from botocore.stub import Stubber

from archdiscov.ingestion.detail_execution import DetailCall, DetailResponse, DetailResult
from archdiscov.ingestion.elbv2.collector import Elbv2Inventory, collect_inventory
from archdiscov.ingestion.elbv2.normalization import (
    Elbv2Normalized,
    _Normalizer,
    normalize_inventory,
)
from archdiscov.ingestion.operations import Elbv2Operation as Op
from archdiscov.models.architecture import UnknownResource
from archdiscov.models.enums import AwsRegion, CoverageStatus, LoadBalancerType, TargetType
from archdiscov.models.resources.load_balancing import (
    Listener,
    ListenerRule,
    LoadBalancer,
    TargetGroup,
)
from tests.support.aws_fakes import ACCOUNT
from tests.support.coverage_data import coverage
from tests.support.elbv2_data import (
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


def cited_path(result, item, pointer):
    return citation(result, item, pointer)[0].configuration_path


def unresolved_paths(result):
    return [value.configuration_path for c in result.coverage for value in c.unresolved_values]


def test_flat_arn_resources_with_precise_facts():
    data = responses()
    zones = data["describe_load_balancers"]["LoadBalancers"][0]["AvailabilityZones"]
    zones[0].update(
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
    data["describe_target_groups"]["TargetGroups"][0].update(
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
    health = data["describe_target_health"]["TargetHealthDescriptions"][0]["TargetHealth"]
    health["Description"] = "Health checks failed"
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
    assert isinstance(result, Elbv2Normalized)
    assert {item.identity.physical_id for item in result.resources} == {LB, LISTENER, RULE, GROUP}
    assert all(not item.children for item in result.resources)
    for item in result.resources:
        document = item.model_dump()
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
    assert lb.attributes["availability_zones"] == zones
    assert lb.attributes["state"] == {"Code": "active"}
    assert cited_path(result, lb, "/subnet_ids/0").endswith(".AvailabilityZones[0].SubnetId")
    assert cited_path(result, lb, "/security_group_ids/0").endswith(".SecurityGroups[0]")
    listener = resource(result, Listener)
    assert listener.load_balancer_arn == LB
    assert cited_path(result, listener, "/load_balancer_arn") == (
        f'Request[{{"LoadBalancerArn":"{LB}"}}].LoadBalancerArn'
    )
    assert [action.order for action in listener.actions] == [20, 1]
    assert [group.weight for group in listener.actions[1].forward_groups] == [5, 95]
    assert citation(result, listener, "/actions/1/forward_groups/1/weight")[0].value == 95
    assert cited_path(result, listener, "/actions/1/forward_groups/0/arn").endswith(
        ".DefaultActions[1].ForwardConfig.TargetGroups[0].TargetGroupArn"
    )
    rule = resource(result, ListenerRule)
    assert rule.listener_arn == LISTENER
    assert cited_path(result, rule, "/priority").endswith(".Rules[0].Priority")
    assert cited_path(result, rule, "/listener_arn") == (
        f'Request[{{"ListenerArn":"{LISTENER}"}}].ListenerArn'
    )
    assert cited_path(result, rule, "/conditions/0").endswith(".Conditions[0]")
    assert citation(result, rule, "/actions/0/target_group_arn")[0].value == GROUP
    group = resource(result, TargetGroup)
    assert group.target_type is TargetType.INSTANCE
    assert group.targets[0].id == "i-web"
    assert group.targets[0].health_state == "unhealthy"
    assert cited_path(result, group, "/targets/0/id").endswith(
        ".TargetHealthDescriptions[0].Target.Id"
    )
    for field, suffix in (
        ("port", "Target.Port"),
        ("availability_zone", "Target.AvailabilityZone"),
        ("health_state", "TargetHealth.State"),
        ("health_reason", "TargetHealth.Reason"),
        ("health_description", "TargetHealth.Description"),
    ):
        assert cited_path(result, group, f"/targets/0/{field}").endswith(suffix)
    assert group.health_check["HealthyThresholdCount"] == 3
    assert citation(result, group, "/health_check/Matcher")[0].value == {"HttpCode": "200-299"}
    assert group.attributes["tags"] == {"team": "infra"}
    assert ".TagDescriptions[" in cited_path(result, group, "/attributes/tags")


def test_secrets_in_summaries_evidence_and_tags_keep_redaction_paths():
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
    lb = resource(result, LoadBalancer)
    assert lb.attributes["tags"] == {"password": "[REDACTED]"}
    assert citation(result, lb, "/attributes/tags")[0].redacted_paths == ["/0/Value"]
    assert resource(result, TargetGroup).attributes["tags"] == {}


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
                    None,
                    {"TargetGroupArn": ""},
                    {"TargetGroupArn": GROUP, "Weight": -1},
                ]
            },
        },
    ]
    data["describe_listeners"]["Listeners"][0]["Protocol"] = "future-protocol"
    rule = data["describe_rules"]["Rules"][0]
    rule["Conditions"] = [None, {"Field": "future-condition"}, *rule["Conditions"]]
    data["describe_target_health"]["TargetHealthDescriptions"][0]["TargetHealth"]["State"] = (
        "future"
    )
    result = normalize(data)
    assert len(result.resources) == 4
    assert resource(result, LoadBalancer).scheme is None
    listener = resource(result, Listener)
    assert listener.port is None and listener.protocol is None
    assert len(listener.actions) == 1
    assert len(listener.actions[0].forward_groups) == 1
    assert listener.actions[0].forward_groups[0].weight is None
    assert cited_path(result, listener, "/actions/0").endswith("DefaultActions[1]")
    assert cited_path(result, listener, "/actions/0/forward_groups/0/arn").endswith(
        ".ForwardConfig.TargetGroups[2].TargetGroupArn"
    )
    normalized_rule = resource(result, ListenerRule)
    assert cited_path(result, normalized_rule, "/conditions/0").endswith(".Conditions[2]")
    assert normalized_rule.attributes["summary"]["Conditions"] == rule["Conditions"]
    assert resource(result, TargetGroup).targets[0].health_state is None
    assert "secret-unknown" not in str(result)
    paths = unresolved_paths(result)
    assert any(path.endswith(".Scheme") for path in paths)
    assert any(path.endswith(".DefaultActions[0].Type") for path in paths)
    assert any(path.endswith(".ForwardConfig.TargetGroups[0]") for path in paths)
    assert any(path.endswith(".ForwardConfig.TargetGroups[1].TargetGroupArn") for path in paths)
    assert any(path.endswith(".ForwardConfig.TargetGroups[2].Weight") for path in paths)
    assert any(path.endswith(".Conditions[1].Field") for path in paths)
    assert any(path.endswith(".TargetHealth.State") for path in paths)
    assert all(
        c.status == CoverageStatus.INCOMPLETE for c in result.coverage if c.unresolved_values
    )


def test_nlb_gateway_and_lambda_do_not_invent_inapplicable_fields():
    data = responses()
    nlb_arn, gateway_arn = LB.replace("/app/", "/net/"), LB.replace("/app/", "/gwy/")
    lb = data["describe_load_balancers"]["LoadBalancers"][0]
    lb.update({"LoadBalancerArn": nlb_arn, "Type": "network"})
    del lb["SecurityGroups"]
    data["describe_load_balancers"]["LoadBalancers"].append(
        {"LoadBalancerArn": gateway_arn, "Type": "gateway"}
    )
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
    assert [r.arn for r in result.resources if isinstance(r, LoadBalancer)] == [nlb_arn]
    assert resource(result, LoadBalancer).security_group_ids is None
    gateway = resource(result, UnknownResource)
    assert gateway.identity.physical_id == gateway_arn
    assert gateway.aws_resource_type == "AWS::ElasticLoadBalancingV2::LoadBalancer"
    assert gateway.attributes["summary"]["Type"] == "gateway"
    assert not any(isinstance(r, ListenerRule) for r in result.resources)
    group = resource(result, TargetGroup)
    assert group.protocol is None and group.port is None and group.vpc_id is None
    assert group.health_check == {"HealthCheckEnabled": False}
    assert group.targets == []
    assert all(
        gateway_arn in path or ".LoadBalancers[1]." in path for path in unresolved_paths(result)
    )


def test_real_stubber_all_six_reads_have_exact_request_scope():
    client = boto3.client(
        "elbv2", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    session = Elbv2Session(client)
    data = responses()
    with Stubber(client) as stub:
        stub.add_response("describe_load_balancers", data["describe_load_balancers"], {})
        stub.add_response("describe_target_groups", data["describe_target_groups"], {})
        stub.add_response("describe_listeners", data["describe_listeners"], {"LoadBalancerArn": LB})
        stub.add_response("describe_rules", data["describe_rules"], {"ListenerArn": LISTENER})
        stub.add_response(
            "describe_target_health", data["describe_target_health"], {"TargetGroupArn": GROUP}
        )
        stub.add_response(
            "describe_tags",
            {
                "TagDescriptions": [
                    {"ResourceArn": arn, "Tags": [{"Key": "team", "Value": "infra"}]}
                    for arn in sorted([LB, LISTENER, RULE, GROUP])
                ]
            },
            {"ResourceArns": sorted([LB, LISTENER, RULE, GROUP])},
        )
        collected = collect_inventory(session, config(), context(session), AwsRegion.US_EAST_1)
        stub.assert_no_pending_responses()
    assert set(collected.results) == set(Op)
    assert all(item.status == CoverageStatus.SUCCESS for item in collected.coverage)
    assert collected.results[Op.DESCRIBE_TARGET_HEALTH].responses[0].path == (
        f'Request[{{"TargetGroupArn":"{GROUP}"}}].Page[0].TargetHealthDescriptions[0]'
    )


def test_both_boundaries_reject_a_foreign_account_identity():
    op = Op.DESCRIBE_LOAD_BALANCERS
    data = responses()
    item = data["describe_load_balancers"]["LoadBalancers"][0]
    item["LoadBalancerArn"] = LB.replace(ACCOUNT, "000000000000")
    discovered = inventory(data)
    assert discovered.results[op].responses == []
    assert any(
        "Invalid resource identity" in p.reason
        for p in discovered.results[op].coverage.unresolved_values
    )

    outcome = coverage()
    raw = Elbv2Inventory(
        {op: DetailResult([DetailResponse(DetailCall(op), item, "Result[0]")], outcome)}
    )
    normalizer = _Normalizer(UUID(int=1), ACCOUNT, AwsRegion.US_EAST_1, raw)
    assert list(normalizer.entries(op, "LoadBalancerArn")) == []
    assert (
        normalizer.coverage[op].unresolved_values[0].reason == "Invalid resource identity; omitted"
    )
    assert outcome.unresolved_values == []
