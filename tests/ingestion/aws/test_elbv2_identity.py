from uuid import UUID

import pytest

from archdiscov.ingestion.detail_execution import DetailCall, DetailResponse, DetailResult
from archdiscov.ingestion.elbv2.collector import Elbv2Inventory, collect_inventory
from archdiscov.ingestion.elbv2.identity import valid_arn
from archdiscov.ingestion.elbv2.normalization import _Normalizer
from archdiscov.ingestion.operations import Elbv2Operation as Op
from archdiscov.models.enums import AwsRegion
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

IDENTITIES = [
    (Op.DESCRIBE_LOAD_BALANCERS, "LoadBalancers", "LoadBalancerArn", LB),
    (Op.DESCRIBE_TARGET_GROUPS, "TargetGroups", "TargetGroupArn", GROUP),
    (Op.DESCRIBE_LISTENERS, "Listeners", "ListenerArn", LISTENER),
    (Op.DESCRIBE_RULES, "Rules", "RuleArn", RULE),
]


@pytest.mark.parametrize("op,key,field,arn", IDENTITIES)
@pytest.mark.parametrize("invalid", [None, 123, "account", "region", "suffix", "kind"])
def test_both_boundaries_reject_invalid_identity(op, key, field, arn, invalid):
    invalid = {
        "account": arn.replace(ACCOUNT, "000000000000"),
        "region": arn.replace("us-east-1", "us-west-2"),
        "suffix": arn + "/extra",
        "kind": arn.replace(":loadbalancer/", ":targetgroup/")
        if field == "LoadBalancerArn"
        else LB,
    }.get(invalid, invalid)
    data = responses()
    data_name = {
        Op.DESCRIBE_LOAD_BALANCERS: "describe_load_balancers",
        Op.DESCRIBE_TARGET_GROUPS: "describe_target_groups",
        Op.DESCRIBE_LISTENERS: "describe_listeners",
        Op.DESCRIBE_RULES: "describe_rules",
    }[op]
    item = data[data_name][key][0]
    item[field] = invalid
    session = Elbv2Session(Elbv2Client(data))
    discovered = collect_inventory(session, config(), context(session), AwsRegion.US_EAST_1)
    assert discovered.results[op].responses == []
    assert any(
        "Invalid resource identity" in p.reason
        for p in discovered.results[op].coverage.unresolved_values
    )
    assert not valid_arn(invalid, field, ACCOUNT, AwsRegion.US_EAST_1)

    outcome = coverage()
    inventory = Elbv2Inventory(
        {op: DetailResult([DetailResponse(DetailCall(op), item, "Result[0]")], outcome)}
    )
    normalizer = _Normalizer(UUID(int=1), ACCOUNT, AwsRegion.US_EAST_1, inventory)
    assert list(normalizer.entries(op, field)) == []
    assert (
        normalizer.coverage[op].unresolved_values[0].reason == "Invalid resource identity; omitted"
    )
    assert outcome.unresolved_values == []


@pytest.mark.parametrize("partition", ["aws", "aws-cn", "aws-us-gov"])
@pytest.mark.parametrize(
    "field,suffix",
    [
        ("LoadBalancerArn", "loadbalancer/app/web-1/123abc"),
        ("LoadBalancerArn", "loadbalancer/net/web-1/123abc"),
        ("LoadBalancerArn", "loadbalancer/gwy/web-1/123abc"),
        ("ListenerArn", "listener/app/web-1/123abc/456def"),
        ("ListenerArn", "listener/net/web-1/123abc/456def"),
        ("RuleArn", "listener-rule/app/web-1/123abc/456def/789abc"),
        ("TargetGroupArn", "targetgroup/web-1/123abc"),
    ],
)
def test_shared_validator_retains_supported_shapes_and_partition_syntax(partition, field, suffix):
    arn = f"arn:{partition}:elasticloadbalancing:us-east-1:{ACCOUNT}:{suffix}"
    assert valid_arn(arn, field, ACCOUNT, AwsRegion.US_EAST_1)
    assert not valid_arn(arn + "\n", field, ACCOUNT, AwsRegion.US_EAST_1)
