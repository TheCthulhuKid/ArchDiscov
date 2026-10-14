"""Shared ELBv2 ARN shapes scoped to the requested account and region."""

import re

from ...models.enums import AwsRegion

_SHAPES = {
    "LoadBalancerArn": r"loadbalancer/(?:app|net|gwy)/[A-Za-z0-9-]+/[A-Za-z0-9]+",
    "ListenerArn": r"listener/(?:app|net)/[A-Za-z0-9-]+/[A-Za-z0-9]+/[A-Za-z0-9]+",
    "RuleArn": r"listener-rule/app/[A-Za-z0-9-]+/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+",
    "TargetGroupArn": r"targetgroup/[A-Za-z0-9-]+/[A-Za-z0-9]+",
}


def valid_arn(value: object, field: str, account_id: str, region: AwsRegion) -> bool:
    """Check syntax and scope only; resource type and parent consistency stay local."""
    prefix = (
        rf"arn:aws[a-z-]*:elasticloadbalancing:{re.escape(region.value)}:"
        rf"{account_id}:"
    )
    return isinstance(value, str) and re.fullmatch(prefix + _SHAPES[field], value) is not None
