from typing import Any
from uuid import UUID

from ...models.enums import AwsRegion
from ..adapter import AdapterContext
from ..configuration_common import ServiceCollection
from ..requests import AwsAccountConfig
from .discovery import collect_inventory
from .normalization import normalize


def collect_ecs(
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    snapshot_id: UUID,
    account_id: str,
    region: AwsRegion,
) -> ServiceCollection:
    inventory = collect_inventory(session, account, context, account_id, region)
    resources, evidence = normalize(inventory, snapshot_id, account_id, region)
    return ServiceCollection(resources=resources, evidence=evidence, coverage=inventory.coverage)
