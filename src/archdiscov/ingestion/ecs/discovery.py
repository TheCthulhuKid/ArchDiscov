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
        key = _KEYS[op]

        def problem(path: str, reason: str) -> None:
            problems.append(UnresolvedValue(configuration_path=path, reason=reason))

        for response in raw.responses:
            page = response.value
            path = f"{response.path}.{key}"
            if not isinstance(page, dict):
                problem(path, "Required result is unavailable")
                continue
            failures = page.get("failures", [])
            if not isinstance(failures, list):
                problem(f"{response.path}.failures", "Malformed ECS failure collection")
            elif failures:
                for index in range(len(failures)):
                    # API reasons/details may contain arbitrary text; do not retain them.
                    problem(f"{response.path}.failures[{index}]", "ECS reported a resource failure")
            value = page.get(key)
            single = op == Op.DESCRIBE_TASK_DEFINITION
            if not isinstance(value, dict if single else list):
                problem(path, "Required result is unavailable")
                continue
            values = [value] if single else value
            retained = set()
            for index, item in enumerate(values):
                item_path = path if single else f"{path}[{index}]"
                if op in _IDENTITIES:
                    param, identity_key = _IDENTITIES[op]
                    arn = item.get(identity_key) if isinstance(item, dict) else None
                    expected = response.call.params[param]
                    expected = [expected] if isinstance(expected, str) else expected
                    if arn not in expected:
                        problem(
                            item_path, "Detail identity is missing or outside the requested scope"
                        )
                        continue
                else:
                    arn = item
                    kind = {
                        Op.LIST_CLUSTERS: "cluster",
                        Op.LIST_SERVICES: "service",
                        Op.LIST_TASKS: "task",
                    }[op]
                    if not valid_arn(arn, kind, account_id, region):
                        problem(item_path, "Discovery identity is unavailable or malformed")
                        continue
                retained.add(arn)
                responses.append(DetailResponse(response.call, item, item_path))
            if op in _IDENTITIES:
                param, _ = _IDENTITIES[op]
                expected = response.call.params[param]
                expected = [expected] if isinstance(expected, str) else expected
                for arn in sorted(set(expected) - retained):
                    problem(path, "A requested resource detail was not returned")
        fields = raw.coverage.model_dump()
        if problems:
            fields.update(status=CoverageStatus.INCOMPLETE, unresolved_values=problems)
        if fields["status"] in {CoverageStatus.SUCCESS, CoverageStatus.INCOMPLETE}:
            fields["result_count"] = len(responses)
        result = DetailResult(responses, Coverage(**fields))
        results[op] = result
        return result

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
    service_details = []
    for cluster in cluster_arns:
        arns = sorted({r.value for r in services.responses if r.call.params["cluster"] == cluster})
        service_details.extend(
            DetailCall(Op.DESCRIBE_SERVICES, {"cluster": cluster, "services": arns[i : i + 10]})
            for i in range(0, len(arns), 10)
        )
    described_services = run(Op.DESCRIBE_SERVICES, service_details, services)
    tasks = run(
        Op.LIST_TASKS,
        [
            DetailCall(Op.LIST_TASKS, {"cluster": arn, "desiredStatus": "RUNNING"}, paginate=True)
            for arn in cluster_arns
        ],
        clusters,
    )
    task_details = []
    for cluster in cluster_arns:
        arns = sorted({r.value for r in tasks.responses if r.call.params["cluster"] == cluster})
        task_details.extend(
            DetailCall(Op.DESCRIBE_TASKS, {"cluster": cluster, "tasks": arns[i : i + 100]})
            for i in range(0, len(arns), 100)
        )
    described_tasks = run(Op.DESCRIBE_TASKS, task_details, tasks)
    definitions = set()
    for result in (described_services, described_tasks):
        for response in result.responses:
            item = response.value
            # Include deployment revisions as well as the service's primary revision.
            candidates = (
                [
                    item.get("taskDefinition"),
                    *[
                        deployment.get("taskDefinition")
                        for deployment in item.get("deployments", [])
                        if isinstance(deployment, dict)
                    ],
                ]
                if isinstance(item.get("deployments", []), list)
                else [item.get("taskDefinition")]
            )
            definitions.update(
                arn for arn in candidates if valid_arn(arn, "task-definition", account_id, region)
            )
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
