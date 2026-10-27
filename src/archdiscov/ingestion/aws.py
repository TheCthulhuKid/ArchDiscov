import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..models.architecture import (
    ArchitectureSnapshot,
    AwsSource,
    Coverage,
    Dependency,
    Evidence,
    RegionArchitecture,
    Resource,
    SnapshotCoverage,
)
from ..models.enums import AwsRegion, AwsService, CoverageStatus
from .adapter import AdapterContext, IngestionError
from .async_collection import AccountCollection, AsyncCollectionCoordinator
from .async_runtime import run_sync
from .aws_sessions import assume_role_parameters, profile_session, role_credentials
from .collection_jobs import CollectedJob, CollectionJob, collect_job, collection_jobs
from .execution_common import COLLECTION_ERRORS, MalformedResponse
from .execution_common import client_config as client_config
from .execution_common import require_allowed as require_allowed
from .execution_common import sanitize_failure as sanitize_failure
from .identity import verify_account_identity
from .operations import ALLOWED_OPERATIONS as ALLOWED_OPERATIONS
from .operations import Ec2Operation
from .region_scope import RegionResolution, authentication_region, resolve_region_scope
from .requests import (
    ACCOUNT_WIDE_SERVICES,
    AwsAccountConfig,
    AwsCollectionRequest,
    RegionScope,
    SourceRequest,
)
from .service_availability import resolve_service_availability
from .service_catalog import client_name as _client_name

EC2_OPERATIONS = (
    (Ec2Operation.DESCRIBE_VPCS, "Vpcs"),
    (Ec2Operation.DESCRIBE_SUBNETS, "Subnets"),
    (Ec2Operation.DESCRIBE_INSTANCES, "Reservations"),
    (Ec2Operation.DESCRIBE_ROUTE_TABLES, "RouteTables"),
    (Ec2Operation.DESCRIBE_INTERNET_GATEWAYS, "InternetGateways"),
    (Ec2Operation.DESCRIBE_NETWORK_ACLS, "NetworkAcls"),
    (Ec2Operation.DESCRIBE_SECURITY_GROUPS, "SecurityGroups"),
    (Ec2Operation.DESCRIBE_SECURITY_GROUP_RULES, "SecurityGroupRules"),
    (Ec2Operation.DESCRIBE_NETWORK_INTERFACES, "NetworkInterfaces"),
    (Ec2Operation.DESCRIBE_NAT_GATEWAYS, "NatGateways"),
    (Ec2Operation.DESCRIBE_EGRESS_ONLY_INTERNET_GATEWAYS, "EgressOnlyInternetGateways"),
    (Ec2Operation.DESCRIBE_VPC_PEERING_CONNECTIONS, "VpcPeeringConnections"),
    (Ec2Operation.DESCRIBE_VPC_ENDPOINTS, "VpcEndpoints"),
    (Ec2Operation.DESCRIBE_MANAGED_PREFIX_LISTS, "PrefixLists"),
)


def default_session_factory(**kwargs: Any) -> Any:
    import boto3

    return boto3.Session(**kwargs)


def _snake(operation: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", operation).lower()


def _session_factory(context: AdapterContext) -> Callable[..., Any]:
    return context.session_factory or default_session_factory


def create_session(account: AwsAccountConfig, context: AdapterContext) -> Any:
    """Create a profile session, then an assumed-role session when configured."""
    factory = _session_factory(context)
    region = account.bootstrap_region.value if account.bootstrap_region else None
    try:
        base = profile_session(factory, account.profile, region)
    except COLLECTION_ERRORS as error:
        raise IngestionError("identity_failed", "could not create the AWS session") from error
    if account.role_arn is None:
        return base
    require_allowed(AwsService.STS, "AssumeRole")
    params = assume_role_parameters(account.role_arn, account.session_name, account.external_id)
    try:
        auth_region = authentication_region(base, account)
        sts = base.client(
            "sts",
            config=client_config(account),
            **({"region_name": auth_region} if auth_region else {}),
        )
        with context.sdk_calls.permit(account.expected_account_id, account.max_in_flight_calls):
            response = sts.assume_role(**params)
        credentials = role_credentials(response)
        if credentials is None:
            raise MalformedResponse("invalid assume-role credentials")
        return factory(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            **({"region_name": auth_region} if auth_region else {}),
        )
    except COLLECTION_ERRORS as error:
        raise IngestionError("identity_failed", "could not assume the configured role") from error


def verify_identity(
    session: Any, account: AwsAccountConfig, context: AdapterContext | None = None
) -> str:
    """Return the verified account ID of the session; abort on failure or mismatch."""
    return verify_account_identity(session, account, context=context).account_id


@dataclass(frozen=True)
class OperationTask:
    region: AwsRegion
    service: AwsService
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    result_key: str | None = None
    paginate: bool = True


@dataclass(frozen=True)
class OperationResult:
    items: list[Any]
    coverage: Coverage


class OperationRunner:
    """Runs allowlisted read operations and reports one coverage record per operation."""

    def __init__(self, session: Any, account: AwsAccountConfig, context: AdapterContext) -> None:
        self._session = session
        self._account = account
        self._context = context
        self._config = client_config(account)
        self._clients: dict[tuple[AwsRegion, AwsService], Any] = {}
        self._seen: set[tuple[AwsRegion, AwsService, str]] = set()
        self._lock = threading.Lock()

    def _client(self, region: AwsRegion, service: AwsService) -> Any:
        with self._lock:
            key = (region, service)
            if key not in self._clients:
                self._clients[key] = self._session.client(
                    _client_name(service), region_name=region.value, config=self._config
                )
            return self._clients[key]

    def _claim(self, task_key: tuple[AwsRegion, AwsService, str]) -> None:
        with self._lock:
            if task_key in self._seen:
                raise IngestionError(
                    "invalid_request", "duplicate operation for account/region/service"
                )
            self._seen.add(task_key)

    def _coverage(self, task: OperationTask, started: Any, **fields: Any) -> Coverage:
        return Coverage(
            account_id=self._account.expected_account_id,
            region=task.region,
            service=task.service,
            operation=task.operation,
            started_at=started,
            completed_at=self._context.clock(),
            **fields,
        )

    def skip(self, task: OperationTask, reason: str) -> OperationResult:
        require_allowed(task.service, task.operation)
        self._claim((task.region, task.service, task.operation))
        now = self._context.clock()
        return OperationResult(
            [], self._coverage(task, now, status=CoverageStatus.SKIPPED, reason=reason)
        )

    def unsupported(self, task: OperationTask, reason: str) -> OperationResult:
        require_allowed(task.service, task.operation)
        self._claim((task.region, task.service, task.operation))
        now = self._context.clock()
        return OperationResult(
            [], self._coverage(task, now, status=CoverageStatus.UNSUPPORTED, reason=reason)
        )

    def run(self, task: OperationTask) -> OperationResult:
        # Reject before any client is created or called.
        require_allowed(task.service, task.operation)
        self._claim((task.region, task.service, task.operation))
        started = self._context.clock()
        items: list[Any] = []
        pages = 0
        try:
            client = self._client(task.region, task.service)
            method = _snake(task.operation)
            if task.paginate and client.can_paginate(method):
                responses: Iterable[Any] = self._context.sdk_calls.pages(
                    client.get_paginator(method).paginate(**task.params),
                    self._account.expected_account_id,
                    self._account.max_in_flight_calls,
                )
            else:
                with self._context.sdk_calls.permit(
                    self._account.expected_account_id, self._account.max_in_flight_calls
                ):
                    responses = [getattr(client, method)(**task.params)]
            for page in responses:
                pages += 1
                if not isinstance(page, dict):
                    raise MalformedResponse("response must be an object")
                if task.result_key is None:
                    items.append(page)
                else:
                    if not isinstance(page.get(task.result_key), list):
                        raise MalformedResponse("response omitted the requested result list")
                    items.extend(page[task.result_key])
        except COLLECTION_ERRORS as error:
            failure = sanitize_failure(error, task.operation)
            if pages == 0:
                coverage = self._coverage(
                    task, started, status=CoverageStatus.FAILED, failures=[failure]
                )
            else:
                coverage = self._coverage(
                    task,
                    started,
                    status=CoverageStatus.INCOMPLETE,
                    result_count=len(items),
                    failures=[failure],
                )
            return OperationResult(items, coverage)
        coverage = self._coverage(
            task, started, status=CoverageStatus.SUCCESS, result_count=len(items)
        )
        return OperationResult(items, coverage)

    def run_many(self, tasks: Iterable[OperationTask]) -> list[OperationResult]:
        """Run tasks with bounded concurrency; results are ordered deterministically."""
        task_list = list(tasks)
        for task in task_list:
            require_allowed(task.service, task.operation)
        with ThreadPoolExecutor(max_workers=self._account.max_concurrency) as pool:
            results = list(pool.map(self.run, task_list))
        return sorted(
            results,
            key=lambda r: (r.coverage.region.value, r.coverage.service.value, r.coverage.operation),
        )


class AwsAdapter:
    """Verifies accounts, then composes selected regional and global configuration."""

    def collect(
        self, request: SourceRequest, *, context: AdapterContext
    ) -> list[ArchitectureSnapshot]:
        outcomes = run_sync(lambda: self.collect_accounts_async(request, context=context))
        for outcome in outcomes:
            if outcome.error is not None:
                raise outcome.error
        return [outcome.snapshot for outcome in outcomes if outcome.snapshot is not None]

    async def collect_accounts_async(
        self, request: SourceRequest, *, context: AdapterContext
    ) -> list[AccountCollection]:
        """Internal scheduling boundary; persistence workflows remain in services."""
        if not isinstance(request, AwsCollectionRequest):
            raise IngestionError("invalid_request", "AWS adapter requires an AWS request")
        coordinator = AsyncCollectionCoordinator(request, context, _session_factory(context))
        return await coordinator.run(self._collect_account_async)

    def _prepare_account(self, account: AwsAccountConfig, context: AdapterContext) -> tuple:
        started = context.clock()
        session = create_session(account, context)
        identity = verify_account_identity(session, account, context=context)
        resolution = resolve_region_scope(
            session, account, partition=identity.partition, context=context
        )
        resolved = account.model_dump()
        resolved.update(regions=resolution.scope.regions, region_scope=RegionScope.EXPLICIT)
        account = AwsAccountConfig.model_validate(resolved)
        available, operations = resolve_service_availability(session, resolution, context)
        snapshot_id = context.uuid_factory()
        jobs = collection_jobs(account, available)
        return account, session, snapshot_id, started, resolution, jobs, operations

    async def _collect_account_async(
        self,
        account: AwsAccountConfig,
        context: AdapterContext,
        coordinator: AsyncCollectionCoordinator,
    ) -> ArchitectureSnapshot:
        (
            account,
            session,
            snapshot_id,
            started,
            resolution,
            jobs,
            operations,
        ) = await coordinator.blocking(self._prepare_account, account, context)
        results = await coordinator.collect_jobs(
            jobs,
            lambda job: collect_job(
                job, session, account, context, snapshot_id, account.expected_account_id
            ),
        )
        return await coordinator.blocking(
            self._assemble_account,
            account,
            context,
            snapshot_id,
            started,
            resolution,
            jobs,
            results,
            operations,
        )

    def _assemble_account(
        self,
        account: AwsAccountConfig,
        context: AdapterContext,
        snapshot_id: UUID,
        started: datetime,
        resolution: RegionResolution,
        jobs: Iterable[CollectionJob],
        results: Iterable[CollectedJob],
        unavailable: list[Coverage],
    ) -> ArchitectureSnapshot:
        from .configuration_relationships import merge_resources as merge_configuration
        from .configuration_relationships import resolve_relationships as resolve_configuration
        from .elbv2.relationships import merge_resources as merge_elbv2
        from .elbv2.relationships import resolve_relationships as resolve_elbv2
        from .iam.relationships import resolve_relationships as resolve_iam
        from .queues.relationships import resolve_relationships as resolve_queue_policies

        order = {job: index for index, job in enumerate(jobs)}
        results = list(results)
        if len(results) != len(order) or {result.job for result in results} != set(order):
            raise ValueError("collection results must contain each planned job exactly once")
        ordered = sorted(results, key=lambda result: order[result.job])
        # Placement and relationship builders may mutate models, never collected inputs.
        ordered = deepcopy(ordered)
        operations = deepcopy(unavailable)
        regions: dict[AwsRegion, RegionArchitecture] = {}
        evidence: dict[str, Evidence] = {}
        dependencies: dict[AwsRegion, list[Dependency]] = {}
        global_resources: list[Resource] = []
        ec2_operations: list[Coverage] = []
        for result in ordered:
            job, collection = result.job, result.collection
            if job.service is AwsService.EC2:
                ec2_operations.extend(collection.coverage)
            else:
                operations.extend(collection.coverage)
            evidence.update(collection.evidence)
            if job.region is AwsRegion.GLOBAL:
                global_resources.extend(collection.resources)
                continue
            architecture = regions.setdefault(job.region, RegionArchitecture(resources=[]))
            if job.service is AwsService.EC2:
                architecture.resources.extend(collection.resources)
            elif job.service is AwsService.ELASTIC_LOAD_BALANCING:
                merge_elbv2(architecture.resources, collection.resources)
            else:
                merge_configuration(architecture.resources, collection.resources)
            if result.dependencies:
                dependencies.setdefault(job.region, []).extend(result.dependencies)
        operations[len(unavailable) : len(unavailable)] = sorted(
            ec2_operations, key=lambda op: (op.region.value, op.service.value, op.operation)
        )
        for region, architecture in regions.items():
            links = resolve_elbv2(architecture.resources, evidence)
            if links:
                dependencies.setdefault(region, []).extend(links)
        configuration_dependencies = resolve_configuration(
            [*global_resources, *(r for region in regions.values() for r in region.resources)],
            evidence,
        )
        for region, links in configuration_dependencies.items():
            dependencies.setdefault(region, []).extend(links)
        iam_dependencies = resolve_iam(
            snapshot_id,
            account.expected_account_id,
            global_resources,
            (resource for region in regions.values() for resource in region.resources),
            evidence,
        )
        for region, links in iam_dependencies.items():
            dependencies.setdefault(region, []).extend(links)
        queue_dependencies = resolve_queue_policies(
            snapshot_id,
            account.expected_account_id,
            global_resources,
            (resource for region in regions.values() for resource in region.resources),
            evidence,
        )
        for region, links in queue_dependencies.items():
            dependencies.setdefault(region, []).extend(links)
        return ArchitectureSnapshot(
            schema_version="2",
            snapshot_id=snapshot_id,
            account_id=account.expected_account_id,
            collection_started_at=started,
            collection_completed_at=context.clock(),
            source=AwsSource(profile=account.profile, role_arn=account.role_arn),
            regions=regions,
            global_resources=global_resources,
            coverage=SnapshotCoverage(
                regions=list(account.regions),
                account_wide=bool(set(account.services) & ACCOUNT_WIDE_SERVICES),
                operations=operations,
            ),
            dependencies_by_region=dependencies,
            evidence=evidence,
            metadata={
                "collection_scope": resolution.scope.to_provenance(),
                "region_resolution": resolution.diagnostics(),
            },
        )
