from typing import Any

from ..models.architecture import Coverage
from ..models.enums import AwsRegion, AwsService, CoverageStatus
from .adapter import AdapterContext
from .operations import (
    Ec2Operation,
    EcsOperation,
    Elbv2Operation,
    LambdaOperation,
    RdsOperation,
    SqsOperation,
)
from .region_scope import RegionResolution, endpoint_regions

REGIONAL_OPERATIONS = {
    AwsService.EC2: tuple(op for op in Ec2Operation if op is not Ec2Operation.DESCRIBE_REGIONS),
    AwsService.ELASTIC_LOAD_BALANCING: tuple(Elbv2Operation),
    AwsService.ECS: tuple(EcsOperation),
    AwsService.LAMBDA: tuple(LambdaOperation),
    AwsService.RDS: tuple(RdsOperation),
    AwsService.SQS: tuple(SqsOperation),
}


def resolve_service_availability(
    session: Any, resolution: RegionResolution, context: AdapterContext
) -> tuple[dict[AwsService, tuple[AwsRegion, ...]], list[Coverage]]:
    """Missing SDK endpoints are unsupported scope, not successful empty inventory."""
    available = {}
    coverage = []
    for service in resolution.scope.services:
        if service not in REGIONAL_OPERATIONS:
            continue
        name = "elbv2" if service is AwsService.ELASTIC_LOAD_BALANCING else service.value
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
