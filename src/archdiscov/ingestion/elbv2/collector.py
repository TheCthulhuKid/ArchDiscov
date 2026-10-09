"""Staged, bounded ELBv2 reads with source-local discovery limitations."""

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from ...models.architecture import Coverage, UnresolvedValue
from ...models.enums import AwsRegion, AwsService, CoverageStatus
from ..adapter import AdapterContext
from ..detail_execution import DetailCall, DetailResponse, DetailResult, DetailRunner
from ..operations import Elbv2Operation as Op
from ..requests import AwsAccountConfig

_KEYS = {
    Op.DESCRIBE_LOAD_BALANCERS: "LoadBalancers",
    Op.DESCRIBE_TARGET_GROUPS: "TargetGroups",
    Op.DESCRIBE_LISTENERS: "Listeners",
    Op.DESCRIBE_RULES: "Rules",
    Op.DESCRIBE_TARGET_HEALTH: "TargetHealthDescriptions",
    Op.DESCRIBE_TAGS: "TagDescriptions",
}
_SHAPES = {
    "LoadBalancerArn": r"loadbalancer/(?:app|net|gwy)/[A-Za-z0-9-]+/[A-Za-z0-9]+",
    "ListenerArn": r"listener/(?:app|net)/[A-Za-z0-9-]+/[A-Za-z0-9]+/[A-Za-z0-9]+",
    "RuleArn": r"listener-rule/app/[A-Za-z0-9-]+/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+",
    "TargetGroupArn": r"targetgroup/[A-Za-z0-9-]+/[A-Za-z0-9]+",
}


@dataclass
class Elbv2Inventory:
    results: dict[Op, DetailResult]

    @property
    def coverage(self) -> list[Coverage]:
        return [result.coverage for result in self.results.values()]


def _limited(result: DetailResult, problems: list[UnresolvedValue]) -> DetailResult:
    if not problems:
        return result
    fields = result.coverage.model_dump()
    fields.update(
        status=CoverageStatus.INCOMPLETE,
        result_count=len(result.responses),
        unresolved_values=[*result.coverage.unresolved_values, *problems],
    )
    return DetailResult(result.responses, Coverage(**fields))


def _problem(path: str, reason: str) -> UnresolvedValue:
    return UnresolvedValue(configuration_path=path, reason=reason)


def _prerequisite(*results: DetailResult) -> str | None:
    unavailable = sorted(
        {
            result.coverage.operation
            for result in results
            if result.coverage.status != CoverageStatus.SUCCESS
        }
    )
    if unavailable:
        return f"Discovery was unavailable or incomplete: {', '.join(unavailable)}"
    return None


class _InventoryRunner(DetailRunner):
    """Retain failed request scope to avoid qualifying unrelated sibling stages."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_calls: list[DetailCall] = []

    def _execute(self, client, call, canonical):
        result = super()._execute(client, call, canonical)
        if result.failure is not None:
            with self._lock:
                self.failed_calls.append(call)
        return result


def collect_inventory(
    session: Any, account: AwsAccountConfig, context: AdapterContext, region: AwsRegion
) -> Elbv2Inventory:
    runner = _InventoryRunner(
        session,
        account,
        context,
        service=AwsService.ELASTIC_LOAD_BALANCING,
        region=region,
        client_name="elbv2",
        pagination="elbv2",
    )
    results: dict[Op, DetailResult] = {}

    def run(op: Op, calls: list[DetailCall], *upstream: DetailResult) -> DetailResult:
        # Keep whole pages until shape validation: absent keys are not empty inventories.
        raw = runner.run(op, calls, prerequisite=_prerequisite(*upstream))
        responses = []
        problems = []
        key = _KEYS[op]
        for response in raw.responses:
            path = f"{response.path}.{key}"
            value = response.value
            if not isinstance(value, dict) or not isinstance(value.get(key), list):
                problems.append(_problem(path, "Required result collection is unavailable"))
                continue
            responses.extend(
                DetailResponse(replace(response.call, result_key=key), item, f"{path}[{index}]")
                for index, item in enumerate(value[key])
            )
        fields = raw.coverage.model_dump()
        if fields["result_count"] is not None:
            fields["result_count"] = len(responses)
        result = _limited(DetailResult(responses, Coverage(**fields)), problems)
        results[op] = result
        return result

    def inventory(op: Op, field: str) -> dict[str, DetailResponse]:
        result = results[op]
        problems = []
        candidates: dict[str, list[DetailResponse]] = {}
        unsafe: set[int] = set()
        tainted: set[str] = set()
        prefix = (
            rf"arn:aws[a-z-]*:elasticloadbalancing:{re.escape(region.value)}:"
            rf"{account.expected_account_id}:"
        )
        for index, response in enumerate(result.responses):
            value = response.value
            arn = value.get(field) if isinstance(value, dict) else None
            valid = isinstance(arn, str) and re.fullmatch(prefix + _SHAPES[field], arn)
            if valid and field == "LoadBalancerArn":
                lb_type = value.get("Type")
                kind = {"application": "app", "network": "net", "gateway": "gwy"}.get(
                    lb_type if isinstance(lb_type, str) else None
                )
                valid = kind is None or arn.split(":", 5)[5].startswith(f"loadbalancer/{kind}/")
            if valid and field == "ListenerArn":
                parent = response.call.params["LoadBalancerArn"]
                valid = value.get("LoadBalancerArn", parent) == parent and arn.split(":", 5)[
                    5
                ].removeprefix("listener/").rsplit("/", 1)[0] == parent.split(":", 5)[
                    5
                ].removeprefix("loadbalancer/")
            if valid and field == "RuleArn":
                parent = response.call.params["ListenerArn"]
                valid = arn.split(":", 5)[5].removeprefix("listener-rule/").rsplit("/", 1)[
                    0
                ] == parent.split(":", 5)[5].removeprefix("listener/")
            if not valid:
                unsafe.add(index)
                if isinstance(arn, str) and re.fullmatch(prefix + _SHAPES[field], arn):
                    tainted.add(arn)
                problems.append(_problem(f"{response.path}.{field}", "Invalid resource identity"))
            else:
                candidates.setdefault(arn, []).append(response)
        safe = {}
        conflicts = set()
        for arn, entries in candidates.items():
            first = entries[0]
            if arn in tainted or any(
                entry.value != first.value or entry.call.params != first.call.params
                for entry in entries[1:]
            ):
                conflicts.add(arn)
                problems.extend(
                    _problem(f"{entry.path}.{field}", "Conflicting resource identity")
                    for entry in entries
                )
            else:
                safe[arn] = first
        retained = [
            response
            for index, response in enumerate(result.responses)
            if index not in unsafe and response.value[field] not in conflicts
        ]
        fields = result.coverage.model_dump()
        if fields["result_count"] is not None:
            fields["result_count"] = len(retained)
        results[op] = _limited(DetailResult(retained, Coverage(**fields)), problems)
        return safe

    run(Op.DESCRIBE_LOAD_BALANCERS, [DetailCall(Op.DESCRIBE_LOAD_BALANCERS, paginate=True)])
    run(Op.DESCRIBE_TARGET_GROUPS, [DetailCall(Op.DESCRIBE_TARGET_GROUPS, paginate=True)])
    lbs = inventory(Op.DESCRIBE_LOAD_BALANCERS, "LoadBalancerArn")
    groups = inventory(Op.DESCRIBE_TARGET_GROUPS, "TargetGroupArn")
    supported_lbs = {
        arn: response
        for arn, response in lbs.items()
        if response.value.get("Type") in ("application", "network")
    }
    supported_groups = {
        arn: response
        for arn, response in groups.items()
        if response.value.get("TargetType") in ("instance", "ip", "alb", "lambda")
    }
    for op, items, supported, field in (
        (Op.DESCRIBE_LOAD_BALANCERS, lbs, supported_lbs, "Type"),
        (Op.DESCRIBE_TARGET_GROUPS, groups, supported_groups, "TargetType"),
    ):
        results[op] = _limited(
            results[op],
            [
                _problem(f"{response.path}.{field}", "Unsupported resource type; summary retained")
                for arn, response in items.items()
                if arn not in supported
            ],
        )
    run(
        Op.DESCRIBE_LISTENERS,
        [
            DetailCall(Op.DESCRIBE_LISTENERS, {"LoadBalancerArn": arn}, paginate=True)
            for arn in sorted(supported_lbs)
        ],
        results[Op.DESCRIBE_LOAD_BALANCERS],
    )
    listeners = inventory(Op.DESCRIBE_LISTENERS, "ListenerArn")
    alb_arns = {
        arn for arn, response in supported_lbs.items() if response.value["Type"] == "application"
    }
    rule_upstream = [results[Op.DESCRIBE_LOAD_BALANCERS]]
    listener_result = results[Op.DESCRIBE_LISTENERS]
    # NLB listeners legitimately have no rules, even if their listener reads failed.
    relevant_problems = [
        problem
        for problem in listener_result.coverage.unresolved_values
        if any(f'"LoadBalancerArn":"{arn}"' in problem.configuration_path for arn in alb_arns)
    ]
    relevant_failures = any(
        call.operation == Op.DESCRIBE_LISTENERS and call.params["LoadBalancerArn"] in alb_arns
        for call in runner.failed_calls
    )
    if alb_arns and (
        relevant_problems
        or relevant_failures
        or listener_result.coverage.status == CoverageStatus.FAILED
    ):
        rule_upstream.append(listener_result)
    run(
        Op.DESCRIBE_RULES,
        [
            DetailCall(Op.DESCRIBE_RULES, {"ListenerArn": arn}, paginate=True)
            for arn, response in sorted(listeners.items())
            if response.call.params["LoadBalancerArn"] in alb_arns
        ],
        *rule_upstream,
    )
    rules = inventory(Op.DESCRIBE_RULES, "RuleArn")
    run(
        Op.DESCRIBE_TARGET_HEALTH,
        [
            DetailCall(Op.DESCRIBE_TARGET_HEALTH, {"TargetGroupArn": arn})
            for arn in sorted(supported_groups)
        ],
        results[Op.DESCRIBE_TARGET_GROUPS],
    )
    arns = sorted(set(supported_lbs) | set(listeners) | set(rules) | set(supported_groups))
    tags = run(
        Op.DESCRIBE_TAGS,
        [
            DetailCall(Op.DESCRIBE_TAGS, {"ResourceArns": arns[index : index + 20]})
            for index in range(0, len(arns), 20)
        ],
        *(
            results[op]
            for op in (
                Op.DESCRIBE_LOAD_BALANCERS,
                Op.DESCRIBE_TARGET_GROUPS,
                Op.DESCRIBE_LISTENERS,
                Op.DESCRIBE_RULES,
            )
        ),
    )
    problems = []
    retained = []
    returned: dict[tuple[str, ...], set[str]] = {}
    tag_entries: dict[str, list[DetailResponse]] = {}
    for response in tags.responses:
        requested = tuple(response.call.params["ResourceArns"])
        value = response.value
        arn = value.get("ResourceArn") if isinstance(value, dict) else None
        if not isinstance(arn, str) or arn not in requested:
            problems.append(_problem(f"{response.path}.ResourceArn", "Unexpected tag identity"))
            continue
        returned.setdefault(requested, set()).add(arn)
        if not isinstance(value.get("Tags"), list) or any(
            not isinstance(tag, dict)
            or not isinstance(tag.get("Key"), str)
            or not isinstance(tag.get("Value"), str)
            for tag in value["Tags"]
        ):
            problems.append(_problem(f"{response.path}.Tags", "Tag collection is unavailable"))
            continue
        tag_entries.setdefault(arn, []).append(response)
    for entries in tag_entries.values():
        if any(entry.value != entries[0].value for entry in entries[1:]):
            problems.extend(
                _problem(f"{entry.path}.Tags", "Conflicting tag replies") for entry in entries
            )
        else:
            retained.extend(entries)
    # Missing replies are source-local limitations, never fabricated empty tag sets.
    for index in range(0, len(arns), 20):
        requested = tuple(arns[index : index + 20])
        canonical = json.dumps(
            {"ResourceArns": list(requested)}, sort_keys=True, separators=(",", ":")
        )
        for arn in sorted(set(requested) - returned.get(requested, set())):
            problems.append(
                _problem(
                    f"Request[{canonical}].Page[0].TagDescriptions[ResourceArn={arn}]",
                    "Requested resource tag reply is unavailable",
                )
            )
    fields = tags.coverage.model_dump()
    if fields["result_count"] is not None:
        fields["result_count"] = len(retained)
    # A failed batch has no successful reads; preserve FAILED rather than claiming a partial read.
    if tags.coverage.status == CoverageStatus.FAILED:
        fields["unresolved_values"] = [*tags.coverage.unresolved_values, *problems]
        results[Op.DESCRIBE_TAGS] = DetailResult(retained, Coverage(**fields))
    else:
        results[Op.DESCRIBE_TAGS] = _limited(DetailResult(retained, Coverage(**fields)), problems)
    return Elbv2Inventory(results)
