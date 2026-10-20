"""Stable resource IDs for test snapshots."""

from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resource_base import ResourceIdentity

ACCOUNT = "123456789012"


def stable_id(
    physical_id: str,
    region: AwsRegion,
    service: AwsService = AwsService.EC2,
    account_id: str = ACCOUNT,
) -> str:
    return ResourceIdentity(
        account_id=account_id, region=region, service=service, physical_id=physical_id
    ).stable_id
