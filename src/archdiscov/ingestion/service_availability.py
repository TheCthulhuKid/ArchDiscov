from typing import Any

from ..models.architecture import Coverage
from ..models.enums import AwsRegion, AwsService, CoverageStatus
from .adapter import AdapterContext
from .region_scope import RegionResolution, endpoint_regions
from .service_catalog import REGIONAL_OPERATIONS as REGIONAL_OPERATIONS
from .service_catalog import client_name


def resolve_service_availability(
    session: Any, resolution: RegionResolution, context: AdapterContext
) -> tuple[dict[AwsService, tuple[AwsRegion, ...]], list[Coverage]]:
    """Missing SDK endpoints are unsupported scope, not successful empty inventory."""
    available = {}
    coverage = []
    for service in resolution.scope.services:
        if service not in REGIONAL_OPERATIONS:
            continue
        name = client_name(service)
        lookup = getattr(session, "get_available_regions", None)
        known = set(
            lookup(name, partition_name=resolution.partition)
            if callable(lookup)
            else endpoint_regions(name, resolution.partition)
        )
        available[service] = tuple(r for r in resolution.scope.regions if r.value in known)
        for region in resolution.scope.regions:
            if region.value in known:
                continue
            for operation in REGIONAL_OPERATIONS[service]:
                now = context.clock()
                coverage.append(
                    Coverage(
                        account_id=resolution.scope.account_id,
                        region=region,
                        service=service,
                        operation=operation,
                        status=CoverageStatus.UNSUPPORTED,
                        started_at=now,
                        completed_at=now,
                        reason=(
                            "no regional service endpoint in SDK metadata; "
                            "availability is not established"
                        ),
                    )
                )
    return available, coverage
