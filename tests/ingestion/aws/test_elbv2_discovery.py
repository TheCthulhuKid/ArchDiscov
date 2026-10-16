from copy import deepcopy

import pytest
from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.elbv2 import Elbv2Inventory, collect_inventory
from archdiscov.ingestion.operations import Elbv2Operation as Op
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion, AwsService

PREFIX = "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
LB = PREFIX + "loadbalancer/app/web/123"
LISTENER = PREFIX + "listener/app/web/123/456"
RULE = PREFIX + "listener-rule/app/web/123/456/789"
GROUP = PREFIX + "targetgroup/web/123"


def data():
    return {
        "describe_load_balancers": {
            "LoadBalancers": [
                {"LoadBalancerArn": LB, "Type": "application"},
            ]
        },
        "describe_target_groups": {
            "TargetGroups": [
                {"TargetGroupArn": GROUP, "TargetType": "instance"},
            ]
        },
        "describe_listeners": {
            "Listeners": [
                {"ListenerArn": LISTENER, "LoadBalancerArn": LB},
            ]
        },
        "describe_rules": {"Rules": [{"RuleArn": RULE}]},
        "describe_target_health": {"TargetHealthDescriptions": [{"Target": {"Id": "i-web"}}]},
    }


class Client:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def __getattr__(self, method):
        def call(**params):
            self.calls.append((method, deepcopy(params)))
            if method == "describe_tags" and method not in self.values:
                return {
                    "TagDescriptions": [
                        {"ResourceArn": arn, "Tags": []} for arn in params["ResourceArns"]
                    ]
                }
            value = self.values[method]
            if callable(value):
                value = value(params)
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)

        return call


class Session:
    def __init__(self, values):
        self.elb = Client(values)
        self.clients = []

    def client(self, name, **kwargs):
        self.clients.append((name, kwargs))
        return self.elb


def collect(values=None):
    session = Session(data() if values is None else values)
    account = AwsAccountConfig(
        profile="test",
        expected_account_id="123456789012",
        regions=(AwsRegion.US_EAST_1,),
        services=(AwsService.ELASTIC_LOAD_BALANCING,),
        max_concurrency=1,
    )
    return collect_inventory(session, account, AdapterContext(), AwsRegion.US_EAST_1), session


def test_all_reads_and_request_specific_paths():
    inventory, session = collect()
    assert isinstance(inventory, Elbv2Inventory)
    assert set(inventory.results) == set(Op)
    assert all(item.status == "success" for item in inventory.coverage)
    assert session.clients[0][0] == "elbv2"
    assert session.clients[0][1]["region_name"] == "us-east-1"
    assert inventory.results[Op.DESCRIBE_RULES].responses[0].path == (
        f'Request[{{"ListenerArn":"{LISTENER}"}}].Page[0].Rules[0]'
    )
    assert [p for m, p in session.elb.calls if m == "describe_tags"] == [
        {"ResourceArns": sorted([LB, LISTENER, RULE, GROUP])},
    ]


@pytest.mark.parametrize(
    "operation,key",
    [
        ("describe_load_balancers", "LoadBalancers"),
        ("describe_target_groups", "TargetGroups"),
        ("describe_listeners", "Listeners"),
        ("describe_rules", "Rules"),
    ],
)
def test_paginated_duplicate_items_retain_citations_but_dedup_requests(operation, key):
    values = data()
    original = values[operation]
    values[operation] = lambda p: {**original, **({} if "Marker" in p else {"NextMarker": "next"})}
    inventory, session = collect(values)
    op = next(op for op in Op if op.value.lower() == operation.replace("_", ""))
    responses = inventory.results[op].responses
    assert len(responses) == 2
    assert responses[1].path.endswith(f"Page[1].{key}[0]")
    assert all(item.status == "success" for item in inventory.coverage)
    assert len([p for m, p in session.elb.calls if m == "describe_target_health"]) == 1
    assert len([p for m, p in session.elb.calls if m == "describe_tags"]) == 1


def test_zero_discovery_and_blocked_discovery():
    values = data()
    values["describe_load_balancers"] = {"LoadBalancers": []}
    values["describe_target_groups"] = {"TargetGroups": []}
    inventory, session = collect(values)
    assert all(c.status == "success" and c.result_count == 0 for c in inventory.coverage)
    assert len(session.elb.calls) == 2
    values["describe_load_balancers"] = EndpointConnectionError(endpoint_url="private source error")
    inventory, _ = collect(values)
    assert inventory.results[Op.DESCRIBE_LISTENERS].coverage.status == "skipped"
    assert inventory.results[Op.DESCRIBE_RULES].coverage.status == "skipped"
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].coverage.status == "success"
    assert (
        "private source error"
        not in inventory.results[Op.DESCRIBE_LOAD_BALANCERS].coverage.model_dump_json()
    )


def test_partial_inventory_keeps_known_reads_and_groups_are_independent():
    values = data()

    def pages(params):
        return (
            EndpointConnectionError(endpoint_url="private")
            if "Marker" in params
            else {
                **data()["describe_load_balancers"],
                "NextMarker": "next",
            }
        )

    values["describe_load_balancers"] = pages
    inventory, _ = collect(values)
    for op in (
        Op.DESCRIBE_LOAD_BALANCERS,
        Op.DESCRIBE_LISTENERS,
        Op.DESCRIBE_RULES,
        Op.DESCRIBE_TAGS,
    ):
        assert inventory.results[op].coverage.status == "incomplete"
        assert inventory.results[op].responses
    assert inventory.results[Op.DESCRIBE_TARGET_GROUPS].coverage.status == "success"
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].coverage.status == "success"


@pytest.mark.parametrize("failed", ["describe_listeners", "describe_rules"])
def test_failed_sibling_retains_successful_reads(failed):
    values = data()
    lb2 = LB.replace("web/123", "other/234")
    listener2 = LISTENER.replace("web/123", "other/234")
    values["describe_load_balancers"]["LoadBalancers"].append(
        {"LoadBalancerArn": lb2, "Type": "application"}
    )
    values["describe_listeners"] = lambda p: (
        EndpointConnectionError(endpoint_url="private")
        if failed == "describe_listeners" and p["LoadBalancerArn"] == lb2
        else {
            "Listeners": [{"ListenerArn": listener2 if p["LoadBalancerArn"] == lb2 else LISTENER}]
        }
    )
    values["describe_rules"] = lambda p: (
        EndpointConnectionError(endpoint_url="private")
        if failed == "describe_rules" and p["ListenerArn"] == listener2
        else {"Rules": [{"RuleArn": RULE}]}
    )
    inventory, _ = collect(values)
    assert inventory.results[Op.DESCRIBE_RULES].coverage.status == "incomplete"
    assert inventory.results[Op.DESCRIBE_RULES].responses
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].coverage.status == "success"
    assert inventory.results[Op.DESCRIBE_TAGS].coverage.status == "incomplete"


def test_nlb_rules_are_legitimate_zero_and_unrelated_listener_failure_does_not_qualify():
    values = data()
    nlb = PREFIX + "loadbalancer/net/tcp/123"
    values["describe_load_balancers"]["LoadBalancers"].append(
        {"LoadBalancerArn": nlb, "Type": "network"}
    )
    values["describe_listeners"] = lambda p: (
        EndpointConnectionError(endpoint_url="private")
        if p["LoadBalancerArn"] == nlb
        else data()["describe_listeners"]
    )
    inventory, _ = collect(values)
    assert inventory.results[Op.DESCRIBE_RULES].coverage.status == "success"
    values["describe_load_balancers"] = {
        "LoadBalancers": [{"LoadBalancerArn": nlb, "Type": "network"}]
    }
    inventory, session = collect(values)
    assert inventory.results[Op.DESCRIBE_RULES].coverage.status == "success"
    assert inventory.results[Op.DESCRIBE_RULES].coverage.result_count == 0
    assert not any(m == "describe_rules" for m, _ in session.elb.calls)


def test_unsupported_summaries_retained_without_unsafe_detail_requests():
    values = data()
    gateway = PREFIX + "loadbalancer/gwy/gateway/123"
    values["describe_load_balancers"] = {
        "LoadBalancers": [{"LoadBalancerArn": gateway, "Type": "gateway"}]
    }
    values["describe_target_groups"]["TargetGroups"][0]["TargetType"] = "future"
    inventory, session = collect(values)
    assert inventory.results[Op.DESCRIBE_LOAD_BALANCERS].responses[0].value["Type"] == "gateway"
    assert inventory.results[Op.DESCRIBE_TARGET_GROUPS].responses[0].value["TargetType"] == "future"
    assert inventory.results[Op.DESCRIBE_LISTENERS].coverage.status == "skipped"
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].coverage.status == "skipped"
    assert len(session.elb.calls) == 2


@pytest.mark.parametrize("bad", ["conflict", "malformed", "account", "region", "parent"])
def test_unsafe_identity_is_omitted_and_qualifies_dependent_discovery(bad):
    values = data()
    if bad == "conflict":
        values["describe_load_balancers"]["LoadBalancers"].append(
            {"LoadBalancerArn": LB, "Type": "network"}
        )
    elif bad == "parent":
        values["describe_listeners"]["Listeners"][0]["LoadBalancerArn"] = "private"
    else:
        values["describe_load_balancers"]["LoadBalancers"][0]["LoadBalancerArn"] = {
            "malformed": "private",
            "account": LB.replace("123456789012", "000000000000"),
            "region": LB.replace("us-east-1", "eu-west-1"),
        }[bad]
    inventory, _ = collect(values)
    op = Op.DESCRIBE_LISTENERS if bad == "parent" else Op.DESCRIBE_LOAD_BALANCERS
    assert inventory.results[op].coverage.status == "incomplete"
    assert not inventory.results[op].responses
    assert inventory.results[op].coverage.unresolved_values
    assert inventory.results[Op.DESCRIBE_RULES].coverage.status == "skipped"


def test_missing_result_field_is_unavailable_not_empty():
    values = data()
    values["describe_target_groups"] = {}
    inventory, _ = collect(values)
    coverage = inventory.results[Op.DESCRIBE_TARGET_GROUPS].coverage
    assert coverage.status == "incomplete"
    assert coverage.unresolved_values[0].configuration_path == "Request[{}].Page[0].TargetGroups"
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].coverage.status == "skipped"


def test_tags_missing_unexpected_and_malformed_are_not_empty_tags():
    values = data()
    values["describe_tags"] = {
        "TagDescriptions": [
            {"ResourceArn": LB, "Tags": []},
            {"ResourceArn": GROUP, "Tags": "private"},
            {"ResourceArn": "private", "Tags": []},
        ]
    }
    inventory, _ = collect(values)
    result = inventory.results[Op.DESCRIBE_TAGS]
    assert result.coverage.status == "incomplete"
    assert [r.value["ResourceArn"] for r in result.responses] == [LB]
    assert len(result.coverage.unresolved_values) == 4
    assert "private" not in result.coverage.model_dump_json()
    assert any(
        f"ResourceArn={LISTENER}" in u.configuration_path for u in result.coverage.unresolved_values
    )


def test_tags_batch_twenty_and_health_for_all_supported_types():
    values = data()
    values["describe_load_balancers"] = {"LoadBalancers": []}
    values["describe_target_groups"] = {
        "TargetGroups": [
            {
                "TargetGroupArn": PREFIX + f"targetgroup/group-{i}/123",
                "TargetType": ("instance", "ip", "alb", "lambda")[i % 4],
            }
            for i in range(21)
        ]
    }
    inventory, session = collect(values)
    assert all(c.status == "success" for c in inventory.coverage)
    batches = [p["ResourceArns"] for m, p in session.elb.calls if m == "describe_tags"]
    assert [len(b) for b in batches] == [20, 1]
    assert len(set(sum(batches, []))) == 21
    assert len([p for m, p in session.elb.calls if m == "describe_target_health"]) == 21


def test_failed_tag_batch_retains_sibling_batch_and_qualifies_missing_resource():
    values = data()
    values["describe_load_balancers"] = {"LoadBalancers": []}
    groups = [PREFIX + f"targetgroup/group-{i:02}/123" for i in range(21)]
    values["describe_target_groups"] = {
        "TargetGroups": [{"TargetGroupArn": arn, "TargetType": "ip"} for arn in groups]
    }
    values["describe_tags"] = lambda p: (
        EndpointConnectionError(endpoint_url="private")
        if len(p["ResourceArns"]) == 1
        else {"TagDescriptions": [{"ResourceArn": arn, "Tags": []} for arn in p["ResourceArns"]]}
    )
    inventory, _ = collect(values)
    result = inventory.results[Op.DESCRIBE_TAGS]
    assert result.coverage.status == "incomplete"
    assert len(result.responses) == 20
    assert len(result.coverage.failures) == 1
    assert len(result.coverage.unresolved_values) == 1
    assert groups[-1] in result.coverage.unresolved_values[0].configuration_path


def test_failed_health_does_not_qualify_independent_tags():
    values = data()
    values["describe_target_health"] = EndpointConnectionError(endpoint_url="private")
    inventory, _ = collect(values)
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].coverage.status == "failed"
    assert inventory.results[Op.DESCRIBE_TAGS].coverage.status == "success"


def test_conflicting_duplicate_tags_are_unavailable():
    values = data()
    values["describe_tags"] = lambda p: {
        "TagDescriptions": [{"ResourceArn": arn, "Tags": []} for arn in p["ResourceArns"]]
        + [{"ResourceArn": LB, "Tags": [{"Key": "team", "Value": "infra"}]}]
    }
    inventory, _ = collect(values)
    result = inventory.results[Op.DESCRIBE_TAGS]
    assert result.coverage.status == "incomplete"
    assert all(response.value["ResourceArn"] != LB for response in result.responses)
    assert len(result.coverage.unresolved_values) == 2


@pytest.mark.parametrize(
    "operation,key",
    [
        ("describe_load_balancers", "LoadBalancers"),
        ("describe_target_groups", "TargetGroups"),
        ("describe_listeners", "Listeners"),
        ("describe_rules", "Rules"),
    ],
)
def test_malformed_inventory_items_are_source_safe_limitations(operation, key):
    values = data()
    values[operation] = {key: [None, "private", {"private": "private"}]}
    inventory, _ = collect(values)
    op = next(op for op in Op if op.value.lower() == operation.replace("_", ""))
    result = inventory.results[op]
    assert result.coverage.status == "incomplete"
    assert len(result.coverage.unresolved_values) == 3
    assert not result.responses
    assert "private" not in result.coverage.model_dump_json()


def test_repeated_marker_retains_page_citations_and_is_incomplete():
    values = data()
    values["describe_load_balancers"] = {
        **values["describe_load_balancers"],
        "NextMarker": "private",
    }
    inventory, session = collect(values)
    result = inventory.results[Op.DESCRIBE_LOAD_BALANCERS]
    assert result.coverage.status == "incomplete"
    assert len(result.responses) == 2
    assert "private" not in result.coverage.model_dump_json()
    assert len([p for m, p in session.elb.calls if m == "describe_load_balancers"]) == 2
