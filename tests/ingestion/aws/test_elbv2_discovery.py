import functools
import operator
from copy import deepcopy

import pytest
from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.elbv2 import collect_inventory
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


def test_blocked_discovery_skips_dependent_reads_only():
    values = data()
    values["describe_load_balancers"] = EndpointConnectionError(endpoint_url="private source error")
    inventory, _ = collect(values)
    assert inventory.results[Op.DESCRIBE_LISTENERS].coverage.status == "skipped"
    assert inventory.results[Op.DESCRIBE_RULES].coverage.status == "skipped"
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].coverage.status == "success"
    assert (
        "private source error"
        not in inventory.results[Op.DESCRIBE_LOAD_BALANCERS].coverage.model_dump_json()
    )


@pytest.mark.parametrize(
    "lb_type,kind,rules",
    [("network", "net", "success"), ("application", "app", "incomplete")],
    ids=["nlb", "alb"],
)
def test_failed_listener_read_qualifies_rules_only_for_an_application_load_balancer(
    lb_type, kind, rules
):
    values = data()
    other = PREFIX + f"loadbalancer/{kind}/other/234"
    values["describe_load_balancers"]["LoadBalancers"].append(
        {"LoadBalancerArn": other, "Type": lb_type}
    )
    values["describe_listeners"] = lambda p: (
        EndpointConnectionError(endpoint_url="private")
        if p["LoadBalancerArn"] == other
        else data()["describe_listeners"]
    )
    inventory, _ = collect(values)
    assert inventory.results[Op.DESCRIBE_LISTENERS].coverage.status == "incomplete"
    assert inventory.results[Op.DESCRIBE_RULES].coverage.status == rules


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


def test_missing_or_malformed_inventory_is_unavailable_not_empty():
    values = data()
    values["describe_target_groups"] = {}
    values["describe_load_balancers"] = {"LoadBalancers": [None, "private", {"private": "private"}]}
    inventory, _ = collect(values)
    groups = inventory.results[Op.DESCRIBE_TARGET_GROUPS].coverage
    assert groups.status == "incomplete"
    assert groups.unresolved_values[0].configuration_path == "Request[{}].Page[0].TargetGroups"
    balancers = inventory.results[Op.DESCRIBE_LOAD_BALANCERS]
    assert balancers.coverage.status == "incomplete"
    assert len(balancers.coverage.unresolved_values) == 3
    assert not balancers.responses
    assert "private" not in balancers.coverage.model_dump_json()
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].coverage.status == "skipped"
    assert inventory.results[Op.DESCRIBE_RULES].coverage.status == "skipped"


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


def test_tags_batch_twenty_and_a_failed_batch_qualifies_its_resources():
    values = data()
    values["describe_load_balancers"] = {"LoadBalancers": []}
    groups = [PREFIX + f"targetgroup/group-{i:02}/123" for i in range(21)]
    values["describe_target_groups"] = {
        "TargetGroups": [
            {"TargetGroupArn": arn, "TargetType": ("instance", "ip", "alb", "lambda")[i % 4]}
            for i, arn in enumerate(groups)
        ]
    }
    inventory, session = collect(values)
    assert all(c.status == "success" for c in inventory.coverage)
    batches = [p["ResourceArns"] for m, p in session.elb.calls if m == "describe_tags"]
    assert [len(b) for b in batches] == [20, 1]
    assert len(set(functools.reduce(operator.iadd, batches, []))) == 21
    assert len([p for m, p in session.elb.calls if m == "describe_target_health"]) == 21

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
