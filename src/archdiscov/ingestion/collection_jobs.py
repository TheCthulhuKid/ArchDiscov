"""Isolated service/scope work; account assembly remains owned by the AWS adapter."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..models.architecture import Dependency
from ..models.enums import AwsRegion, AwsService
from .adapter import AdapterContext
from .configuration_common import ServiceCollection
from .requests import AwsAccountConfig


@dataclass(frozen=True)
class CollectionJob:
    service: AwsService
    region: AwsRegion


@dataclass(frozen=True)
class CollectedJob:
    job: CollectionJob
    collection: ServiceCollection
    dependencies: list[Dependency] = field(default_factory=list)


def collection_jobs(
    account: AwsAccountConfig, available: dict[AwsService, tuple[AwsRegion, ...]]
) -> list[CollectionJob]:
    from .configuration_services import CONFIGURATION_SERVICES

    jobs = []
    for service in (AwsService.EC2, AwsService.ELASTIC_LOAD_BALANCING):
        if service not in account.services:
            continue
        regions = available.get(service, ())
        jobs.extend(CollectionJob(service, region) for region in regions)
    if AwsService.IAM in account.services:
        jobs.append(CollectionJob(AwsService.IAM, AwsRegion.GLOBAL))
    jobs.extend(
        CollectionJob(service, region)
        for region in account.regions
        for service in CONFIGURATION_SERVICES
        if service in account.services and region in available.get(service, ())
    )
    return jobs


def collect_job(
    job: CollectionJob,
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    snapshot_id: UUID,
    account_id: str,
) -> CollectedJob:
    if job.service is AwsService.EC2:
        from .network import normalize_region
        from .network.collector import read_region

        reads = read_region(session, account, context, job.region)
        normalized = normalize_region(
            snapshot_id,
            account_id,
            job.region,
            reads.items,
            reads.unavailable,
            reads.prefix_list_entries,
            reads.transit_gateway_tables,
        )
        return CollectedJob(
            job,
            ServiceCollection(
                normalized.architecture.resources, normalized.evidence, reads.coverage
            ),
            normalized.dependencies,
        )
    if job.service is AwsService.ELASTIC_LOAD_BALANCING:
        from .elbv2 import collect_inventory
        from .elbv2.normalization import normalize_inventory

        inventory = collect_inventory(session, account, context, job.region)
        normalized = normalize_inventory(snapshot_id, account_id, job.region, inventory)
        collection = ServiceCollection(
            normalized.resources, normalized.evidence, normalized.coverage
        )
    elif job.service is AwsService.IAM:
        from .iam import collect_iam

        normalized = collect_iam(session, account, context, snapshot_id, account_id)
        collection = ServiceCollection(
            normalized.resources, normalized.evidence, normalized.coverage
        )
    else:
        from .configuration_services import collect_configuration_service

        collection = collect_configuration_service(
            job.service, session, account, context, snapshot_id, account_id, job.region
        )
    return CollectedJob(job, collection)
