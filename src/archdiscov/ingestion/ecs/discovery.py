"""Bounded ECS inventory reads, retaining raw pages only until source selection."""

import re
from dataclasses import dataclass
from typing import Any

from ...models.architecture import Coverage, UnresolvedValue
from ...models.enums import AwsRegion, AwsService, CoverageStatus
from ..adapter import AdapterContext
from ..detail_execution import DetailCall, DetailResponse, DetailResult, DetailRunner
from ..operations import EcsOperation as Op
from ..requests import AwsAccountConfig

_KEYS = {
    Op.LIST_CLUSTERS: "clusterArns",
    Op.DESCRIBE_CLUSTERS: "clusters",
    Op.LIST_SERVICES: "serviceArns",
    Op.DESCRIBE_SERVICES: "services",
    Op.LIST_TASKS: "taskArns",
    Op.DESCRIBE_TASKS: "tasks",
    Op.DESCRIBE_TASK_DEFINITION: "taskDefinition",
}
_IDENTITIES = {
    Op.DESCRIBE_CLUSTERS: ("clusters", "clusterArn"),
    Op.DESCRIBE_SERVICES: ("services", "serviceArn"),
    Op.DESCRIBE_TASKS: ("tasks", "taskArn"),
    Op.DESCRIBE_TASK_DEFINITION: ("taskDefinition", "taskDefinitionArn"),
}


@dataclass
class EcsInventory:
    results: dict[Op, DetailResult]

    @property
    def coverage(self) -> list[Coverage]:
        return [result.coverage for result in self.results.values()]


def valid_arn(value: Any, kind: str, account_id: str, region: AwsRegion) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(
            rf"arn:aws(?:-[a-z0-9]+)*:ecs:{re.escape(region.value)}:{account_id}:"
            rf"{kind}/[A-Za-z0-9_./:-]+",
            value,
        )
        is not None
    )


def _prerequisite(results: tuple[DetailResult, ...]) -> str | None:
    unavailable = sorted(
        result.coverage.operation
        for result in results
        if result.coverage.status != CoverageStatus.SUCCESS
    )
    return (
        "Discovery was unavailable or incomplete: " + ", ".join(unavailable)
        if unavailable
        else None
    )


_LIST_KINDS = {Op.LIST_CLUSTERS: "cluster", Op.LIST_SERVICES: "service", Op.LIST_TASKS: "task"}


def _unresolved(path: str, reason: str) -> UnresolvedValue:
    return UnresolvedValue(configuration_path=path, reason=reason)


def _requested(response: DetailResponse, op: Op) -> list:
    param, _ = _IDENTITIES[op]
    expected = response.call.params[param]
    return [expected] if isinstance(expected, str) else expected


def _identity(
    op: Op, response: DetailResponse, item: Any, account_id: str, region: AwsRegion
) -> tuple[Any, str | None]:
    """The item's ARN, and why the item cannot be retained, or None when it can."""
    if op not in _IDENTITIES:
        if not valid_arn(item, _LIST_KINDS[op], account_id, region):
            return item, "Discovery identity is unavailable or malformed"
        return item, None
    _, identity_key = _IDENTITIES[op]
    arn = item.get(identity_key) if isinstance(item, dict) else None
    if arn not in _requested(response, op):
        return arn, "Detail identity is missing or outside the requested scope"
    return arn, None


def _failure_problems(page: dict, path: str) -> list[UnresolvedValue]:
    failures = page.get("failures", [])
    if not isinstance(failures, list):
        return [_unresolved(f"{path}.failures", "Malformed ECS failure collection")]
    # API reasons/details may contain arbitrary text; do not retain them.
    return [
        _unresolved(f"{path}.failures[{index}]", "ECS reported a resource failure")
        for index in range(len(failures))
    ]


def _page_items(
    op: Op, response: DetailResponse, account_id: str, region: AwsRegion
) -> tuple[list[DetailResponse], list[UnresolvedValue]]:
    """The page's items that have a valid identity, and the page's problems."""
    key = _KEYS[op]
    page = response.value
    path = f"{response.path}.{key}"
    if not isinstance(page, dict):
        return [], [_unresolved(path, "Required result is unavailable")]
    problems = _failure_problems(page, response.path)
    single = op == Op.DESCRIBE_TASK_DEFINITION
    value = page.get(key)
    if not isinstance(value, dict if single else list):
        return [], [*problems, _unresolved(path, "Required result is unavailable")]
    items = []
    retained = set()
    for index, item in enumerate([value] if single else value):
        item_path = path if single else f"{path}[{index}]"
        arn, reason = _identity(op, response, item, account_id, region)
        if reason is not None:
            problems.append(_unresolved(item_path, reason))
            continue
        retained.add(arn)
        items.append(DetailResponse(response.call, item, item_path))
    if op in _IDENTITIES:
        # One unresolved value per requested detail that was not returned.
        problems += [
            _unresolved(path, "A requested resource detail was not returned")
            for _ in set(_requested(response, op)) - retained
        ]
    return items, problems


def _stage_result(
    coverage: Coverage, responses: list[DetailResponse], problems: list[UnresolvedValue]
) -> DetailResult:
    fields = coverage.model_dump()
    if problems:
        fields.update(status=CoverageStatus.INCOMPLETE, unresolved_values=problems)
    if fields["status"] in {CoverageStatus.SUCCESS, CoverageStatus.INCOMPLETE}:
        fields["result_count"] = len(responses)
    return DetailResult(responses, Coverage(**fields))


def _per_cluster(
    op: Op, listed: DetailResult, cluster_arns: list[str], param: str, size: int
) -> list[DetailCall]:
    """Calls describing each cluster's listed ARNs, `size` at a time."""
    calls = []
    for cluster in cluster_arns:
        arns = sorted({r.value for r in listed.responses if r.call.params["cluster"] == cluster})
        calls.extend(
            DetailCall(op, {"cluster": cluster, param: arns[index : index + size]})
            for index in range(0, len(arns), size)
        )
    return calls


def _definition_candidates(item: dict) -> list[Any]:
    # Include deployment revisions as well as the service's primary revision.
    deployments = item.get("deployments", [])
    if not isinstance(deployments, list):
        return [item.get("taskDefinition")]
    return [
        item.get("taskDefinition"),
        *(
            deployment.get("taskDefinition")
            for deployment in deployments
            if isinstance(deployment, dict)
        ),
    ]


def collect_inventory(
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    account_id: str,
    region: AwsRegion,
) -> EcsInventory:
    runner = DetailRunner(
        session,
        account,
        context,
        service=AwsService.ECS,
        region=region,
        client_name="ecs",
        pagination="nextToken",
    )
    results: dict[Op, DetailResult] = {}

    def run(op: Op, calls: list[DetailCall], *upstream: DetailResult) -> DetailResult:
        raw = runner.run(op, calls, prerequisite=_prerequisite(upstream))
        responses = []
        problems = []
        for response in raw.responses:
            items, found = _page_items(op, response, account_id, region)
            responses += items
            problems += found
        results[op] = _stage_result(raw.coverage, responses, problems)
        return results[op]

    clusters = run(Op.LIST_CLUSTERS, [DetailCall(Op.LIST_CLUSTERS, paginate=True)])
    cluster_arns = sorted({response.value for response in clusters.responses})
    run(
        Op.DESCRIBE_CLUSTERS,
        [
            DetailCall(Op.DESCRIBE_CLUSTERS, {"clusters": cluster_arns[i : i + 100]})
            for i in range(0, len(cluster_arns), 100)
        ],
        clusters,
    )
    services = run(
        Op.LIST_SERVICES,
        [DetailCall(Op.LIST_SERVICES, {"cluster": arn}, paginate=True) for arn in cluster_arns],
        clusters,
    )
    described_services = run(
        Op.DESCRIBE_SERVICES,
        _per_cluster(Op.DESCRIBE_SERVICES, services, cluster_arns, "services", 10),
        services,
    )
    tasks = run(
        Op.LIST_TASKS,
        [
            DetailCall(Op.LIST_TASKS, {"cluster": arn, "desiredStatus": "RUNNING"}, paginate=True)
            for arn in cluster_arns
        ],
        clusters,
    )
    described_tasks = run(
        Op.DESCRIBE_TASKS, _per_cluster(Op.DESCRIBE_TASKS, tasks, cluster_arns, "tasks", 100), tasks
    )
    definitions = {
        arn
        for result in (described_services, described_tasks)
        for response in result.responses
        for arn in _definition_candidates(response.value)
        if valid_arn(arn, "task-definition", account_id, region)
    }
    run(
        Op.DESCRIBE_TASK_DEFINITION,
        [
            DetailCall(Op.DESCRIBE_TASK_DEFINITION, {"taskDefinition": arn})
            for arn in sorted(definitions)
        ],
        described_services,
        described_tasks,
    )
    return EcsInventory(results)
