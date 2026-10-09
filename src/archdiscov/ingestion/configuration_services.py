from typing import Any
from uuid import UUID

from ..models.enums import AwsRegion, AwsService
from .adapter import AdapterContext, IngestionError
from .configuration_common import ServiceCollection
from .requests import AwsAccountConfig

CONFIGURATION_SERVICES = (AwsService.ECS, AwsService.LAMBDA, AwsService.RDS, AwsService.SQS)


def collect_configuration_service(
    service: AwsService,
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    snapshot_id: UUID,
    account_id: str,
    region: AwsRegion,
) -> ServiceCollection:
    from .database import collect_rds
    from .ecs import collect_ecs
    from .functions import collect_lambda
    from .queues import collect_sqs

    collectors = {
        AwsService.ECS: collect_ecs,
        AwsService.LAMBDA: collect_lambda,
        AwsService.RDS: collect_rds,
        AwsService.SQS: collect_sqs,
    }
    if service not in collectors:
        raise IngestionError("invalid_request", "service has no configuration collector")
    return collectors[service](session, account, context, snapshot_id, account_id, region)
