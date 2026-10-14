from importlib import import_module
from typing import Any
from uuid import UUID

from ..models.enums import AwsRegion, AwsService
from .adapter import AdapterContext, IngestionError
from .configuration_common import ServiceCollection
from .requests import AwsAccountConfig
from .service_catalog import CONFIGURATION_SERVICES as CONFIGURATION_SERVICES
from .service_catalog import SERVICE_CATALOG


def collect_configuration_service(
    service: AwsService,
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    snapshot_id: UUID,
    account_id: str,
    region: AwsRegion,
) -> ServiceCollection:
    definition = SERVICE_CATALOG.get(service)
    if definition is None or definition.configuration_collector is None:
        raise IngestionError("invalid_request", "service has no configuration collector")
    module, name = definition.configuration_collector
    collector = getattr(import_module(f".{module}", package=__package__), name)
    return collector(session, account, context, snapshot_id, account_id, region)
