"""Independent RDS inventory reads; Describe responses already contain configuration."""

from typing import Any
from uuid import UUID

from ...models.enums import AwsRegion, AwsService
from ..adapter import AdapterContext
from ..configuration_common import ServiceCollection
from ..detail_execution import DetailCall, DetailRunner
from ..operations import RdsOperation as Op
from ..requests import AwsAccountConfig
from .normalization import normalize


def collect_rds(
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    snapshot_id: UUID,
    account_id: str,
    region: AwsRegion,
) -> ServiceCollection:
    runner = DetailRunner(
        session,
        account,
        context,
        service=AwsService.RDS,
        region=region,
        client_name="rds",
        pagination="rds",
    )
    results = {
        operation: runner.run(operation, [DetailCall(operation, paginate=True)])
        for operation in (
            Op.DESCRIBE_DB_INSTANCES,
            Op.DESCRIBE_DB_CLUSTERS,
            Op.DESCRIBE_DB_SUBNET_GROUPS,
        )
    }
    return normalize(results, snapshot_id, account_id, region)
